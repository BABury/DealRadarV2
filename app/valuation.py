"""AI-taxatie: comparatieve methode + inkomstenbenadering.

Werkwijze (zelfde model als een taxateur, maar geautomatiseerd):
1. Objectdata uit BAG/Kadaster (via analyse.py) of uit de eigen listing-rij.
2. Referenties uit de eigen FlipRadar-database: actuele vraagprijzen van
   vergelijkbare objecten in dezelfde stad, gewogen op gelijkenis
   (m², bouwjaar, wijk). NB: dit zijn vraagprijzen, geen NVM-transacties —
   daarom wordt een vraagprijs->transactie-correctie toegepast.
3. Variabelen (afwerking, onderhoud, ligging) worden automatisch ingeschat
   en zijn door de gebruiker te overrulen via query-parameters.

Alle bedragen zijn indicaties, geen gevalideerde taxaties.
"""
from __future__ import annotations

import statistics

LABELS = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F", "G"]
FINISH_NAMES = {1: "eenvoudig", 2: "gedateerd", 3: "gemiddeld", 4: "goed/modern", 5: "hoogwaardig"}
MAINT_NAMES = {1: "slecht", 2: "matig", 3: "redelijk", 4: "goed", 5: "uitstekend"}

ASK_TO_DEAL = 0.97      # vraagprijs -> verwachte transactieprijs
FINISH_PCT = 0.04       # ±4% per afwerkingsniveau
LABEL_PCT = 0.015       # ±1,5% per labelstap
MAINT_PCT = 0.02        # ±2% per onderhoudsniveau t.o.v. 3
LOC_PCT = 0.025         # ±2,5% per liggingsniveau t.o.v. 3
GROSS_YIELD = 0.045     # aanname bruto aanvangsrendement voor huurindicatie
MAX_REFS = 8


def _label_idx(label: str) -> int:
    label = (label or "").strip().upper()
    return LABELS.index(label) if label in LABELS else LABELS.index("C")


def _finish_from_listing(row: dict) -> int:
    """Afwerkingsniveau 1-5 uit onderhoudsveld, flags en energielabel."""
    m = (row.get("maintenance") or "").lower()
    flags = row.get("flags") or {}
    if flags.get("casco"):
        return 1
    if "slecht" in m:
        return 1
    if "matig" in m or flags.get("verbouw"):
        return 2
    if "uitstekend" in m or "goed" in m:
        return 4
    li = _label_idx(row.get("energy_label"))
    if li <= LABELS.index("B"):
        return 4
    if li >= LABELS.index("F"):
        return 2
    return 3


def _maint_from_listing(row: dict) -> int:
    m = (row.get("maintenance") or "").lower()
    if "slecht" in m:
        return 1
    if "matig" in m:
        return 2
    if "uitstekend" in m:
        return 5
    if "goed" in m:
        return 4
    return 3


def _refs_for(city: str, exclude_url: str | None, neighbourhood: str = "") -> list[dict]:
    from .db import Listing, SessionLocal
    with SessionLocal() as s:
        q = (s.query(Listing)
             .filter(Listing.city.ilike(city or ""),
                     Listing.price.isnot(None), Listing.price > 0,
                     Listing.living_area.isnot(None), Listing.living_area > 0))
        if exclude_url:
            q = q.filter(Listing.url != exclude_url)
        rows = [r.to_dict() for r in q.all()]
    # zelfde wijk eerst, daarna rest
    rows.sort(key=lambda r: (0 if neighbourhood and r.get("neighbourhood") == neighbourhood else 1))
    return rows[:MAX_REFS * 2]


def _weight(ref: dict, area: float, year: int | None, neighbourhood: str) -> float:
    w = 1.0 / (1.0 + abs((ref.get("living_area") or area) - area) / 40.0)
    if year and ref.get("build_year"):
        w *= 1.0 / (1.0 + abs(ref["build_year"] - year) / 40.0)
    if neighbourhood and ref.get("neighbourhood") == neighbourhood:
        w *= 1.5
    return w


def estimate_inputs(subject: dict, refs: list[dict]) -> dict:
    """AI-inschatting van de aanpasbare variabelen."""
    if refs:
        fins = sorted(_finish_from_listing(r) for r in refs)
        ref_finish = fins[len(fins) // 2]
    else:
        ref_finish = 3
    finish = subject.get("_finish_hint") or ref_finish
    return {
        "finish": finish,
        "maintenance": subject.get("_maint_hint") or 3,
        "location": 3,
        "index_pct": 4.0,   # marktindex %/jaar (voor evt. oudere data)
        "opex_pct": 22,     # exploitatiekosten % van bruto huur
    }


def comparative_valuation(subject: dict, refs: list[dict], inputs: dict,
                          exclude_ids: set[int] | None = None) -> dict:
    """subject: address, city, neighbourhood, living_area, plot_area,
    build_year, energy_label (+ optioneel asking_price)."""
    exclude_ids = exclude_ids or set()
    area = float(subject.get("living_area") or 0)
    if not area:
        return {"error": "Geen woonoppervlak bekend — taxatie niet mogelijk."}

    finish = int(inputs["finish"])
    maint = int(inputs["maintenance"])
    loc = int(inputs["location"])
    label_i = _label_idx(subject.get("energy_label"))
    year = subject.get("build_year")
    nbh = subject.get("neighbourhood") or ""

    rows, used = [], []
    for r in refs:
        m2p = (r.get("price") or 0) / (r.get("living_area") or 1)
        c_deal = ASK_TO_DEAL
        c_fin = 1 + FINISH_PCT * (finish - _finish_from_listing(r))
        c_lbl = 1 + LABEL_PCT * (_label_idx(r.get("energy_label")) - label_i)
        c_yr = 1.0
        if year and r.get("build_year"):
            c_yr = 1 + max(-0.03, min(0.03, (year - r["build_year"]) * 0.0004))
        corr = c_deal * c_fin * c_lbl * c_yr
        adj = m2p * corr
        w = _weight(r, area, year, nbh)
        incl = r.get("id") not in exclude_ids
        rows.append({
            "id": r.get("id"), "address": r.get("address"),
            "neighbourhood": r.get("neighbourhood") or "",
            "price": r.get("price"), "living_area": r.get("living_area"),
            "price_m2": round(m2p), "finish": _finish_from_listing(r),
            "energy_label": r.get("energy_label") or "",
            "build_year": r.get("build_year"),
            "correction_pct": round((corr - 1) * 100, 1),
            "adjusted_m2": round(adj), "weight": round(w, 3),
            "included": incl, "url": r.get("url"), "is_demo": r.get("is_demo"),
        })
        if incl:
            used.append((adj, w))
    rows.sort(key=lambda x: -x["weight"])
    rows = rows[:MAX_REFS]
    used_ids = {x["id"] for x in rows if x["included"]}
    used = [(x["adjusted_m2"], x["weight"]) for x in rows if x["included"]]

    if len(used) < 2:
        return {"error": f"Te weinig referenties in {subject.get('city')} "
                         f"({len(used)}) — minimaal 2 nodig.", "refs": rows}

    tot_w = sum(w for _, w in used)
    m2_base = sum(a * w for a, w in used) / tot_w
    m2_subject = m2_base * (1 + MAINT_PCT * (maint - 3)) * (1 + LOC_PCT * (loc - 3))
    value = m2_subject * area
    plot = float(subject.get("plot_area") or 0)
    plot_premium = max(0.0, plot - 150) * 200 if plot else 0.0
    value = round((value + plot_premium) / 1000) * 1000

    adjs = [a for a, _ in used]
    mean = statistics.mean(adjs)
    sd = statistics.pstdev(adjs)
    cv = sd / mean if mean else 0
    band = max(0.05, min(0.15, cv))
    n = len(used)
    confidence = ("hoog" if n >= 5 and cv < 0.08 else
                  "middel" if n >= 3 and cv < 0.13 else "laag")

    # inkomstenbenadering (indicatief)
    rent_m2 = inputs.get("rent_m2") or round(m2_subject * GROSS_YIELD / 12, 2)
    rent_month = round(rent_m2 * area / 5) * 5
    rent_year = rent_month * 12
    opex = float(inputs["opex_pct"]) / 100
    bar = rent_year / value * 100 if value else 0
    nar = rent_year * (1 - opex) / value * 100 if value else 0

    out = {
        "value": value,
        "value_low": round(value * (1 - band) / 1000) * 1000,
        "value_high": round(value * (1 + band) / 1000) * 1000,
        "m2_price": round(m2_subject),
        "confidence": confidence,
        "cv_pct": round(cv * 100, 1),
        "n_refs": n,
        "inputs": {**inputs, "finish_name": FINISH_NAMES.get(finish, ""),
                   "maintenance_name": MAINT_NAMES.get(maint, ""),
                   "rent_m2": rent_m2},
        "refs": rows,
        "income": {"rent_month": rent_month, "rent_year": rent_year,
                   "bar_pct": round(bar, 2), "nar_pct": round(nar, 2),
                   "opex_pct": inputs["opex_pct"]},
        "disclaimer": ("Indicatie o.b.v. vraagprijzen van vergelijkbare objecten in de "
                       "eigen database (gecorrigeerd naar transactieniveau), BAG/Kadaster "
                       "open data en ingeschatte variabelen — geen gevalideerde taxatie."),
    }
    asking = subject.get("asking_price")
    if asking:
        out["asking_price"] = asking
        out["margin"] = round(value - asking)
        out["margin_pct"] = round((value - asking) / asking * 100, 1)
    return out


def valuate(q: str = "", listing_id: int | None = None,
            overrides: dict | None = None, exclude: str = "") -> dict:
    """Hoofdingang. q = adres/Funda-link, of listing_id uit eigen database.
    overrides: area/plot/year/label/finish/maintenance/location/opex/rent_m2."""
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    subject: dict = {}
    sources: list[str] = []

    if listing_id:
        from .db import Listing, SessionLocal
        with SessionLocal() as s:
            row = s.get(Listing, listing_id)
            if not row:
                return {"error": f"Listing {listing_id} niet gevonden."}
            d = row.to_dict()
        subject = {
            "address": d["address"], "city": d["city"],
            "neighbourhood": d.get("neighbourhood") or "",
            "living_area": d["living_area"], "plot_area": d.get("plot_area"),
            "build_year": d.get("build_year"), "energy_label": d.get("energy_label"),
            "asking_price": d.get("price"), "url": d.get("url"),
            "_finish_hint": _finish_from_listing(d), "_maint_hint": _maint_from_listing(d),
        }
        sources.append("eigen database (scrape)")
    elif q:
        from .analyse import _bag_data, _lookup_address, _parse_funda_url, _perceel
        adres = _lookup_address(_parse_funda_url(q.strip()))
        if not adres:
            return {"error": f"Adres niet gevonden voor '{q}'."}
        x, y = adres["_x"], adres["_y"]
        bag = _bag_data(x, y, str(adres.get("adresseerbaarobject_id", "")))
        perceel = _perceel(x, y)
        subject = {
            "address": adres.get("weergavenaam"), "city": adres.get("woonplaatsnaam"),
            "neighbourhood": adres.get("buurtnaam") or "",
            "living_area": bag.get("woonoppervlak"), "plot_area": perceel.get("perceel_m2"),
            "build_year": bag.get("bouwjaar"), "energy_label": "",
        }
        sources += ["Kadaster/BAG (PDOK)", "Kadastrale kaart (PDOK)"]
    else:
        return {"error": "Geef een adres (q) of listing_id op."}

    # gebruikers-overrides op objectdata
    for k_src, k_dst in (("area", "living_area"), ("plot", "plot_area"),
                         ("year", "build_year"), ("label", "energy_label")):
        if k_src in overrides:
            subject[k_dst] = overrides[k_src]

    refs = _refs_for(subject.get("city") or "", subject.get("url"),
                     subject.get("neighbourhood") or "")
    sources.append(f"referenties: eigen database ({subject.get('city')})")

    inputs = estimate_inputs(subject, refs)
    for k in ("finish", "maintenance", "location", "opex_pct", "index_pct", "rent_m2"):
        if k in overrides:
            inputs[k] = overrides[k]

    exclude_ids = {int(i) for i in exclude.split(",") if i.strip().isdigit()}
    result = comparative_valuation(subject, refs, inputs, exclude_ids)
    result["subject"] = {k: v for k, v in subject.items() if not k.startswith("_")}
    result["sources"] = sources
    result["ai_estimated"] = [k for k in ("finish", "maintenance", "location")
                              if k not in overrides]
    return result
