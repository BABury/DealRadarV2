# FlipRadar — backlog

Status 3 juli 2026: Funda-bron live (440 objecten, 12 steden). Veilingbronnen nog rood.

## Prioriteit 1 — veilingbronnen werkend krijgen

De drie veilingsites zijn client-side gerenderd; de scrapers hebben het echte
data-endpoint nodig. Per bron, zonder redeploy te fixen via env vars:

1. **Biedboek** — open biedboek.nl in Chrome → DevTools (Cmd+Opt+I) → tab
   Network → filter op `api` → herlaad → zoek het request dat de objectenlijst
   als JSON teruggeeft → kopieer de volledige URL → zet in Railway als
   `BIEDBOEK_API`. De scraper herkent gangbare JSON-vormen automatisch.
2. **Veilingnotaris** — zelfde aanpak; geeft de site server-side HTML met een
   andere URL-structuur, zet die dan in `VEILINGNOTARIS_URL`. Werkt dat niet,
   dan heeft de scraper een aanpassing nodig (stuur de HTML naar Claude).
3. **BOG Auctions** — idem, `BOG_AUCTIONS_URL`.

Faalt een bron structureel → optie: aparte Playwright-worker (rendert JS),
kan later als tweede Railway-service.

## Prioriteit 2 — volledige dekking

- `FUNDA_MAX_PER_CITY=500` zetten zodat de nachtrun (6:00) de volledige
  voorraad opbouwt i.p.v. 40 per stad.
- Volledige stedenlijst uit recap.py overnemen in `FUNDA_CITIES` (34 steden).
- Na ~1 week draaien wordt prijsverlaging-detectie vanzelf actief (vergelijkt
  runs met elkaar).

## Prioriteit 3 — verrijking (de echte voorsprong)

- **EP-online** (publieke energielabel-database) koppelen: label per adres,
  ook als de makelaar het niet vermeldt.
- **Kadaster koopsommen**: laatste transactieprijs + eigendomsduur per object
  (lang eigendom = overwaarde + gemotiveerde verkoper).
- **WOZ-waarden** (wozwaardeloket) naast vraagprijs leggen.
- België fase 2: immoweb.be + openbareverkoop.be scrapers.

## Prioriteit 4 — app-verbeteringen

- Wachtwoord op het dashboard (nu is de link publiek toegankelijk).
- Voortgangsbalk tijdens scrapen (stad X van Y) i.p.v. alleen de indicator.
- E-mail/pushnotificatie bij nieuwe hot lead (score ≥ 60).
- Export naar Excel voor dealbesprekingen.

## Huishouden

- [ ] Demo-data wissen: `curl -X POST https://flipradar-production-e016.up.railway.app/api/purge-demo`
- [ ] Oude Google service-account key intrekken (Google Cloud Console → IAM →
  Service Accounts → Keys) — vervangen door Postgres, niet meer nodig.

## v5+ roadmap (toegevoegd 4 juli)

- [x] v5: adres-analyse endpoint (/api/analyse) — PDOK Locatieserver + BAG WFS +
  Kadastrale kaart + CBS KWB 2025, vergunningvrij-indicatie (Bbl) en
  waarde/kosten-potentie uit eigen comps. Alles keyless, live geverifieerd.
- [ ] v6: buurt-groeimodel — CBS KWB 2004-2025 delta's per buurt (WOZ-groei als
  target; instroom 25-34 jaar, huishoudens met kinderen, inkomen als features).
  Leidende indicatoren: verjonging + olievlek-effect naast dure buurten.
  Onderbouwing: PBL "De prijs van de plek", CBS longread huizenprijzen 2013+.
- [ ] v7: omgevingsplan-koppeling — DSO Ruimtelijke Plannen API (gratis key
  aanvragen via developer.omgevingswet.overheid.nl) -> bestemming, bouwhoogte,
  bebouwingspercentage per locatie.
- [ ] WOZ per adres: wozwaardeloket heeft sessie-mechanisme; endpoint nog
  uitzoeken (env var WOZ_API voorbereid in gedachten). CBS buurt-WOZ dekt het af.
