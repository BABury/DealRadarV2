# FlipRadar

Data-gedreven vastgoed-sourcing: scrapet Funda (via pyfunda mobiele API) en Nederlandse
veilingplatforms, slaat alles op in Postgres, scoort elk object op flip-potentie en
toont de kansen in een live dashboard.

## Railway deploy (±15 minuten)

1. Zet deze map in een GitHub-repo (`git init && git add . && git commit -m "init"`, push).
2. Ga naar railway.app → **New Project → Deploy from GitHub repo** → kies de repo.
   Railway herkent de Dockerfile automatisch.
3. In het project: **+ New → Database → PostgreSQL**. Railway zet `DATABASE_URL`
   automatisch als je hem koppelt: service → Variables → **Add Reference → DATABASE_URL**.
4. Service → Settings → **Generate Domain** → je hebt een publieke link.
5. Klaar. Bij eerste start laadt de app demo-data zodat het dashboard direct werkt.

## Environment variables (alles optioneel)

| Variabele | Default | Betekenis |
|---|---|---|
| `DATABASE_URL` | sqlite lokaal | Postgres-URL (Railway reference) |
| `SEED_ON_START` | `1` | Demo-data laden als db leeg is |
| `SCRAPE_ON_START` | `0` | Direct scrapen bij opstart |
| `SCRAPE_HOUR` | `6` | Dagelijkse scrape (uur, NL-tijd) |
| `FUNDA_CITIES` | shortlist | Komma-gescheiden steden |
| `FUNDA_MAX_PER_CITY` | `100000` | Detail-calls per stad per run (standaard: alles) |
| `FUNDA_MAX_PRICE` | `2000000` | Prijsplafond |
| `BENCHMARK_MONTHS` | `12` | Horizon verkocht-data voor benchmarks |
| `BENCHMARK_MIN_WIJK` | `15` | Min. verkochte objecten voor wijk-benchmark |
| `BENCHMARK_MIN_STAD` | `8` | Min. verkochte objecten voor stad-benchmark |
| `SEGMENT_SMALL_MAX` / `SEGMENT_MID_MAX` | `50` / `100` | Segmentgrenzen (m²) |
| `SOLD_SCRAPE_DAY` / `SOLD_SCRAPE_HOUR` | `sun` / `3` | Wekelijkse verkocht-scrape |
| `SOLD_SCRAPE_ON_START` | `1` | Bij lege verkocht-tabel direct benchmark-scrape draaien |
| `SOLD_MAX_PAGES_PER_CITY` | `400` | Max. zoekpagina's verkocht per stad |
| `VEILINGNOTARIS_URL` / `BOG_AUCTIONS_URL` / `BIEDBOEK_API` | zie code | Bron-endpoints overriden |

## API

- `GET /` — dashboard
- `GET /api/opportunities?city=&source=&min_score=&q=` — gescoorde objecten
- `GET /api/stats` — totalen
- `GET /api/sources` — laatste run-status per bron (fouten zichtbaar!)
- `GET /api/benchmarks?city=` — marktscorebord: €/m² p25/mediaan/p75 per stad/wijk × segment
- `POST /api/refresh` — scrape nu (draait op achtergrond)
- `POST /api/refresh-sold` — verkocht-scrape nu (voedt de benchmarks)
- `POST /api/purge-demo` — demo-data verwijderen
- `POST /api/rescore` — scores herberekenen

## Lokaal draaien

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app.main:app --reload
# → http://localhost:8000
```

## Belangrijke aantekeningen

- **Demo-data**: rijen met `demo`-badge zijn fictief; verwijder ze vóór echt gebruik
  met `POST /api/purge-demo`. Zeg het er eerlijk bij als je ze in een demo toont.
- **Veilingsites zijn client-side gerenderd** (React). De scrapers proberen bekende
  endpoints; faalt een bron, dan zie je dat onderaan het dashboard (bronstatus) met
  foutmelding. Fix zonder redeploy: zet `BIEDBOEK_API` e.d. als env var — open de
  site in Chrome → DevTools → Network → filter `api` → kopieer het JSON-endpoint.
- **Funda ToS** staat scraping formeel niet toe; pyfunda gebruikt de mobiele API met
  vertragingen. Houd volumes beperkt (dat doet `FUNDA_MAX_PER_CITY`).
- **Scores zijn signalen, geen taxaties** — de score rangschikt waar je als eerste
  naar kijkt, niet wat je biedt.
- **Oude Google service-account key**: de sheets-pipeline is vervangen door Postgres.
  Trek de key `funda-scraper-...json` in via Google Cloud Console (IAM → Service
  Accounts → Keys) — hij is niet meer nodig en rondslingeren is een risico.

## Update juli 2026 — scenario-engine, Top 5 en scrape-fix

**Waarom miste FlipRadar objecten?** Het detail-budget (`FUNDA_MAX_PER_CITY`) werd
elke dag opnieuw besteed aan dezelfde eerste N zoekresultaten, en het budget lekte
weg naar steden buiten de regio (Arnhem, Utrecht, Haarlem...). Beide zijn gefixt:
bekende objecten krijgen nu alleen een gratis prijs-update (prijshistorie blijft
werken) zodat het volledige budget naar NIEUWE objecten gaat, en de standaard
stedenlijst is regio Eindhoven (12 gemeenten). Tip: verwijder op Railway de
`FUNDA_CITIES`-variabele (of zet er je eigen regiolijst in) en zet
`FUNDA_MAX_PER_CITY` op bijv. 60.

**Nieuw:**
- `app/scenarios.py` — Vastgoed Scan-engine: flip / splitsen / verhuur bij
  €1.000–2.500/m² verbouwkosten. Verkoopwaarden schalen mee met de stadsmediaan
  uit de eigen database (uplift-factoren per profiel).
- Profielen (wekelijks aanpasbaar, meerdere runs vergelijkbaar):
  `GET/POST /api/profiles` — seeds: standaard, conservatief, agressief.
- `GET /api/scenarios?listing_id=&profile=` — scenariotabel per object.
- `GET /api/top5?profile=&n=&cities=` — Top 5 regio Eindhoven op beste
  scenarioresultaat.
- Dashboard: Top 5-blok met profielkeuze en aannames-editor; scenariotabel in
  het detailpaneel van elk object (naast BAG/Kadaster-analyse en AI-taxatie).

## Update juli 2026 (2) — verkocht-benchmarks per wijk × segment

**Waarom:** vraagprijzen van actief aanbod zeggen wat verkopers hopen; verkochte
objecten zeggen wat de markt betaalt. En kleine objecten zijn per m² structureel
duurder dan grote, en Amsterdam-Zuid is geen Zuidoost. Daarom wordt de
prijs/m²-score nu berekend tegen de mediaan van VERKOCHTE objecten in dezelfde
wijk én hetzelfde grootte-segment (klein <50 / midden 50–100 / groot >100 m²),
met een getrapte fallback: wijk-verkocht → stad-verkocht → stad-actief.

**Hoe:**
- `app/benchmarks.py` — percentielen (p25/mediaan/p75) per (stad|wijk) × segment
  uit `sold_listings`; horizon `BENCHMARK_MONTHS` (12 mnd).
- Verkocht-scrape (`scrape_funda_sold`) gebruikt ALLEEN zoekresultaten — geen
  detail-calls, dus ±15 objecten per request. Draait wekelijks (zo 03:00) en
  automatisch bij eerste start als de tabel leeg is.
- Nieuw scoresignaal: groot perceel bij woning ≥80 m² (perceel ≥2/3/5× woon-
  oppervlak = +4/+8/+12) — bijbouw-/uitbouwpotentie.
- Keywords uitgebreid: optoppen, uitbouw, bijgebouw, bouwkavel, kluswoning,
  vergunning verleend, e.d.
- Dashboard: 📊 Marktscorebord per stad (segmenten + wijken), kolommen
  "Markt €/m²" en "t.o.v. markt" per object.
- Let op: Funda toont bij verkocht de laatste VRAAGprijs, niet de Kadaster-
  transactieprijs. In een overbiedingsmarkt zijn de scores dus conservatief.

**Railway:** verwijder de env-var `FUNDA_MAX_PER_CITY` (of zet hem hoog) zodat
alle objecten per gemeente meegenomen worden — de code-default is nu onbeperkt.
Bestaande database migreert automatisch (nieuwe kolommen + tabellen bij start).
