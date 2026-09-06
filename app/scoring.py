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

from .benchmarks import BenchmarkMap
from .db import Listing, SessionLocal

AUCTION_SOURCES = {"veilingnotaris", "bog_auctions", "biedboek"}


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

    if l.flag_splitsvergunning:
        pts += 15; bd.append("splitsingsvergunning (+15)")
    elif l.flag_splits_bouwkundig or l.flag_splits_kadastraal:
        pts += 10; bd.append("splitsbaar (+10)")

    hist = json.loads(l.price_history or "[]")
    drops = [h for h in hist if h.get("to", 0) < h.get("from", 0)]
    if len(drops) >= 2:
        pts += 10; bd.append(f"{len(drops)}x prijsverlaging (+10)")
    elif len(drops) == 1:
        pts += 6; bd.append("prijsverlaging (+6)")

    if l.source in AUCTION_SOURCES:
        pts += 8; bd.append("veiling — gemotiveerde verkoop (+8)")

    return min(pts, 100), bd, bench_info


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
