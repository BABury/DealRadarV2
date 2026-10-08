"""Flip-score — prijs/m² wordt gescoord tegen de juiste marktbenchmark:
wijk-verkocht → stad-verkocht → stad-actief (zie benchmarks.py). Kleine
objecten worden dus niet meer 'goedkoop' gerekend tegen grote (segmenten
<50 / 50–100 / >100 m²) en Amsterdam-Zuid niet tegen Zuidoost.

Score (max 100):
  prijs/m² onder segment-benchmark 0-25
  energielabel E/F/G               0-15
  onderhoud matig/slecht           0-10
  verbouwkansen (stapelbaar)       0-20
  groot perceel (bijbouwpotentie)  0-12
  bouwjaar (voor 1970)             0-10
  geen erfpacht                    5
  splitsbaar                       0-15
  gemengd pand (profiel De Pijp)   0-24
  prijsverlaging gezien            0-10
  veiling (gemotiveerde verkoop)   8
"""
from __future__ import annotations

import json
import statistics

import os

from .benchmarks import BenchmarkMap
from .db import Listing, SessionLocal, days_on_market
from .split import analyse as split_analyse

# Moeten gelijk lopen met min_app_m2 / go_verlies_pct in scenarios.DEFAULT_PARAMS
SPLIT_MIN_APP_M2 = float(os.getenv("SPLIT_MIN_APP_M2", "50"))
SPLIT_VERKEER_PCT = float(os.getenv("SPLIT_VERKEER_PCT", "10"))

AUCTION_SOURCES = {"vastgoedveiling", "veilingnotaris", "bog_auctions", "biedboek"}


def city_medians(listings: list[Listing]) -> dict[str, float]:
    """Legacy-hulp (scenario-engine gebruikt city_medians_all)."""
    per_city: dict[str, list[float]] = {}
    for l in listings:
        if l.city and l.price_m2 and l.price_m2 > 0:
            per_city.setdefault(l.city.lower(), []).append(l.price_m2)
    return {c: statistics.median(v) for c, v in per_city.items() if len(v) >= 3}


def score_listing(l: Listing, bm: BenchmarkMap) -> tuple[int, list[str], dict]:
    pts = 0
    bd: list[str] = []
    bench_info = {"label": "", "median": None, "discount": None}

    bench, label = bm.lookup(l.city, l.neighbourhood, l.postcode, l.living_area)
    if l.price_m2 and bench and bench.median:
        disc = (bench.median - l.price_m2) / bench.median * 100
        bench_info = {"label": label, "median": bench.median,
                      "discount": round(disc, 1)}
        for cutoff, p in ((30, 25), (20, 18), (10, 10), (5, 5)):
            if disc >= cutoff:
                pts += p
                bd.append(f"prijs/m² -{disc:.0f}% vs {label} (+{p})")
                break
        else:
            if disc <= -15:
                bd.append(f"prijs/m² +{-disc:.0f}% BOVEN {label}")

    # Waarschuwing bij de korting: bestaat het metrage voor een flink deel uit
    # ruimte die niet als woning verkoopt, dan is prijs/m² misleidend laag.
    if l.flag_lage_ruimte and l.living_area and (l.overige_inpandig or 0) >= 0.2 * l.living_area:
        bd.append("⚠ veel berging/souterrain in het metrage — prijs/m² lijkt lager dan hij is")

    label_pts = {"G": 15, "F": 12, "E": 9, "D": 5, "C": 2}
    el = (l.energy_label or "").upper().strip()
    if el in label_pts:
        pts += label_pts[el]
        bd.append(f"energielabel {el} (+{label_pts[el]})")

    ond = (l.maintenance or "").lower()
    if "slecht" in ond:
        pts += 10; bd.append("onderhoud slecht (+10)")
    elif "matig" in ond:
        pts += 6; bd.append("onderhoud matig (+6)")

    v = 0
    if l.flag_casco:        v += 8; bd.append("casco (+8)")
    if l.flag_verbouw:      v += 5; bd.append("verbouwkans (+5)")
    if l.flag_ontwikkeling: v += 4; bd.append("ontwikkelkans (+4)")
    if l.flag_dakopbouw:    v += 4; bd.append("dakopbouw/optoppen (+4)")
    if l.flag_vliering:     v += 4; bd.append("vliering/bergzolder genoemd (+4)")
    if l.flag_uitbreiden:   v += 3; bd.append("uitbreiden (+3)")
    pts += min(v, 20)

    # ── Gemengd pand ──────────────────────────────────────────────────────
    # Een heel pand waarin al woningen zitten, met bedrijfsruimte eronder.
    # Dit telt apart en zwaar, omdat het de vergunningsroute bepaalt: de
    # bestaande woningen hoeven niet gevormd te worden en de bedrijfsruimte
    # wordt een woning via functiewijziging. In Amsterdam ontloop je daarmee
    # de eis dat nieuw gevormde woningen gemiddeld 100 m² moeten zijn.
    g = 0
    if l.flag_geheel_pand:       g += 6;  bd.append("geheel pand (+6)")
    if l.flag_gemengd_bg:        g += 10; bd.append("bedrijfsruimte op begane grond — functiewijziging i.p.v. woningvorming (+10)")
    if l.flag_meerdere_woningen: g += 8;  bd.append("meerdere bestaande woningen in het pand (+8)")
    if l.flag_leeg_opgeleverd:   g += 5;  bd.append("leeg opgeleverd (+5)")
    if g:
        pts += min(g, 24)
        if l.flag_gemengd_bg and l.flag_meerdere_woningen:
            bd.append("↳ check de BAG op gebruiksdoel per verblijfsobject")

    # Onbenutte inpandige ruimte = de vliering/berging waar een heel
    # appartement uit kan. Dit telt apart en zwaar: het voegt oppervlak TOE,
    # terwijl splitsen bestaand oppervlak alleen verdeelt.
    oi = l.overige_inpandig or 0
    # Rem: noemt de tekst een souterrain of beperkte stahoogte, dan is een
    # deel van die meters berging die nooit voor woonprijzen verkoopt. In De
    # Pijp was dat 79 van de 273,7 m²; vol meetellen maakte het pand ruim twee
    # ton mooier dan het was. Halve punten, en het staat in de onderbouwing.
    demping = 0.5 if l.flag_lage_ruimte else 1.0
    if oi >= 60:
        p = int(20 * demping); pts += p; bd.append(f"onbenutte ruimte {oi:.0f} m² (+{p})")
    elif oi >= 35:
        p = int(14 * demping); pts += p; bd.append(f"onbenutte ruimte {oi:.0f} m² (+{p})")
    elif oi >= 20:
        p = int(8 * demping); pts += p; bd.append(f"onbenutte ruimte {oi:.0f} m² (+{p})")
    # Tweede spoor: veel inhoud t.o.v. woonoppervlak betekent hoogte die
    # nergens als woonruimte meetelt — meestal precies die kap.
    elif l.inhoud_m3 and l.living_area and l.living_area > 0:
        verhouding = l.inhoud_m3 / l.living_area
        if verhouding >= 4.2:
            pts += 10; bd.append(f"veel inhoud ({verhouding:.1f} m³/m²): onbenutte hoogte (+10)")
        elif verhouding >= 3.6:
            pts += 5; bd.append(f"ruime inhoud ({verhouding:.1f} m³/m²) (+5)")

    # Groot perceel bij een volwaardige woning = bijbouw-/uitbouwpotentie
    # (vergunningvrij bouwen schaalt mee met het bebouwingsgebied).
    if (l.living_area or 0) >= 80 and l.plot_area and l.living_area:
        ratio = l.plot_area / l.living_area
        if ratio >= 5:
            pts += 12; bd.append(f"XL perceel {l.plot_area:.0f} m² ({ratio:.1f}× woning) (+12)")
        elif ratio >= 3:
            pts += 8; bd.append(f"groot perceel {l.plot_area:.0f} m² ({ratio:.1f}× woning) (+8)")
        elif ratio >= 2:
            pts += 4; bd.append(f"ruim perceel {l.plot_area:.0f} m² (+4)")

    if l.build_year:
        if l.build_year < 1930:
            pts += 10; bd.append(f"bouwjaar {l.build_year} (+10)")
        elif l.build_year < 1950:
            pts += 7; bd.append(f"bouwjaar {l.build_year} (+7)")
        elif l.build_year < 1970:
            pts += 4; bd.append(f"bouwjaar {l.build_year} (+4)")

    if not l.erfpacht:
        pts += 5; bd.append("geen erfpacht (+5)")

    # Splitsen = de grootste waardesprong. Naast wat de advertentie zegt, ook
    # de fysieke potentie: hoeveel appartementen passen er in het oppervlak?
    sa = split_analyse(l.to_dict(), SPLIT_MIN_APP_M2, SPLIT_VERKEER_PCT)
    if sa["status"] == "vergunning":
        pts += 15; bd.append(f"splitsingsvergunning, {sa['units']} app. (+15)")
    elif sa["status"] == "genoemd":
        pts += 12; bd.append(f"splitsbaar volgens advertentie, {sa['units']} app. (+12)")
    elif sa["status"] == "potentieel":
        p = 10 if sa["units"] >= 3 else 6
        pts += p; bd.append(f"splitspotentie ~{sa['units']} app. (+{p})")

    hist = json.loads(l.price_history or "[]")
    drops = [h for h in hist if h.get("to", 0) < h.get("from", 0)]
    if len(drops) >= 2:
        pts += 10; bd.append(f"{len(drops)}x prijsverlaging (+10)")
    elif len(drops) == 1:
        pts += 6; bd.append("prijsverlaging (+6)")

    # Lang te koop = gemotiveerde verkoper. Vaak het EERSTE signaal, nog
    # voordat de vraagprijs officieel omlaag gaat.
    dom = days_on_market(l.published)
    if dom is not None:
        for cutoff, p in ((180, 12), (120, 9), (90, 6), (60, 3)):
            if dom >= cutoff:
                pts += p
                bd.append(f"{dom} dagen te koop (+{p})")
                break

    # Oordeel van het agent-team. De Lezer beoordeelt de tekst, de Criticus
    # zoekt bezwaren; samen begrensd op -35..+20 (zie agents/criticus.py), zodat
    # de harde cijfers de basis blijven en het team bijstuurt.
    if l.ai_punten is not None or l.ai_samenvatting:
        p = max(-35, min(20, int(l.ai_punten or 0)))
        pts += p
        kern = (l.ai_samenvatting or "oordeel agent-team").strip()
        teken = "+" if p > 0 else ""
        # Ook bij 0 punten tonen: dan heeft het team het gezien en niets
        # bijzonders gevonden, en dat is óók informatie.
        bd.append(f"🤖 {kern[:110]} ({teken}{p})")
    advies = {"laten_lopen": "🤖 criticus: laten lopen",
              "uitzoeken": "🤖 criticus: eerst uitzoeken"}.get(l.ai_advies or "")
    if advies:
        bd.append(advies)

    ctx = (l.context or "").lower()
    if l.source in AUCTION_SOURCES:
        if "[executieveiling]" in ctx:
            pts += 12; bd.append("executieveiling — gedwongen verkoop (+12)")
        else:
            pts += 8; bd.append("veiling — gemotiveerde verkoop (+8)")
    # Huurder blijft na de veiling: niet leeg te verbouwen/splitsen/verkopen
    if "huurbeding ingeroepen" in ctx:
        pts -= 20; bd.append("⚠ huurbeding ingeroepen — huurder blijft zitten (−20)")

    return max(0, min(pts, 100)), bd, bench_info


def compute_scores() -> int:
    """Herbereken alle scores tegen de actuele benchmarks.
    Benchmarks worden eerst ververst zodat ook de actief-aanbod-fallback
    (vóór de eerste verkocht-scrape) altijd actueel is."""
    from .benchmarks import compute_benchmarks
    try:
        compute_benchmarks()
    except Exception as e:
        print(f"[scoring] benchmark-refresh mislukt: {e}", flush=True)
    bm = BenchmarkMap()
    with SessionLocal() as s:
        listings = s.query(Listing).all()
        for l in listings:
            score, bd, bench = score_listing(l, bm)
            l.flip_score = score
            l.score_breakdown = " · ".join(bd)
            l.bench_label = bench["label"] or ""
            l.bench_median = bench["median"]
            l.discount_pct = bench["discount"]
        s.commit()
        return len(listings)
