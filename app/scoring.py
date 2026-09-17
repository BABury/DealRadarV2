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
    if l.flag_uitbreiden:   v += 3; bd.append("uitbreiden (+3)")
    pts += min(v, 20)

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
