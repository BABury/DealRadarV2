"""Adres-analyse: adres of Funda-link in -> potentie-rapport uit.

Databronnen (allemaal gratis, geen API-key, live geverifieerd juli 2026):
- PDOK Locatieserver: adres -> BAG-ids, buurt/wijk, RD-coordinaten
- PDOK BAG WFS: verblijfsobject (woonoppervlak), pand (bouwjaar, footprint)
- PDOK Kadastrale kaart WFS: perceeloppervlak
- CBS Kerncijfers wijken en buurten 2025 (86165NED): buurtstatistieken

Berekeningen zijn indicaties, geen taxaties. Vergunningvrij-regels: Bbl
(50% van eerste 100 m2 bebouwingsgebied + 20% van de rest, aanbouw max 4 m).
"""
from __future__ import annotations

import re

import requests

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
KAD_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"
CBS_ODATA = "https://opendata.cbs.nl/ODataApi/odata/86165NED/TypedDataSet"

HEADERS = {"User-Agent": "FlipRadar/1.0 (vastgoedanalyse)"}
TIMEOUT = 20

BOUWKOSTEN_M2 = 2400   # aanbouw, indicatief incl. btw
WAARDEFACTOR_AANBOUW = 0.75  # extra m2 telt niet 1-op-1 mee in waarde


def _get_json(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _wfs(base: str, type_name: str, bbox: tuple, count: int = 10) -> list[dict]:
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": type_name, "outputFormat": "application/json",
        "count": count, "srsName": "EPSG:28992",
        "bbox": ",".join(str(round(v, 1)) for v in bbox),
    }
    data = _get_json(base, params)
    return data.get("features", [])


def _polygon_area(geom: dict) -> float:
    """Shoelace op de buitenring; coordinaten zijn in meters (RD)."""
    if not geom:
        return 0.0
    if geom.get("type") == "Polygon":
        rings = [geom["coordinates"][0]]
    elif geom.get("type") == "MultiPolygon":
        rings = [p[0] for p in geom["coordinates"]]
    else:
        return 0.0
    total = 0.0
    for ring in rings:
        s = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            s += x1 * y2 - x2 * y1
        total += abs(s) / 2
    return total


def _point_in_geom(x: float, y: float, geom: dict) -> bool:
    """Ray casting op de buitenring(en)."""
    if not geom:
        return False
    polys = ([geom["coordinates"]] if geom.get("type") == "Polygon"
             else geom.get("coordinates", []))
    for poly in polys:
        ring = poly[0]
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        if inside:
            return True
    return False


def _parse_funda_url(q: str) -> str:
    """'https://www.funda.nl/koop/eindhoven/huis-...-hastelweg-112/' -> 'hastelweg 112 eindhoven'"""
    m = re.search(r"funda\.nl/(?:[a-z]+/)?koop/([a-z\-]+)/(?:huis|appartement)[^/]*?-((?:[a-z]+-)+\d+[a-z]?)/?", q)
    if not m:
        return q
    city = m.group(1).replace("-", " ")
    rest = m.group(2)
    # laatste deel is huisnummer, alles daarvoor straatnaam (na evt. id-prefix)
    parts = rest.split("-")
    street_num = " ".join(parts)
    street_num = re.sub(r"^\d+ ", "", street_num)  # funda-id wegstrippen
    return f"{street_num} {city}"


# ── deelstappen ──────────────────────────────────────────────────

def _lookup_address(q: str) -> dict | None:
    data = _get_json(LOCATIESERVER, {"q": q, "rows": 1, "fq": "type:adres"})
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return None
    d = docs[0]
    m = re.search(r"POINT\(([\d.]+) ([\d.]+)\)", d.get("centroide_rd", ""))
    if not m:
        return None
    d["_x"], d["_y"] = float(m.group(1)), float(m.group(2))
    return d


def _bag_data(x: float, y: float, vbo_id: str) -> dict:
    out: dict = {}
    bbox = (x - 4, y - 4, x + 4, y + 4)
    try:
        vbos = _wfs(BAG_WFS, "bag:verblijfsobject", bbox, count=20)
        match = next((f for f in vbos
                      if str(f["properties"].get("identificatie")) == vbo_id), None)
        vbo = match or (vbos[0] if vbos else None)
        if vbo:
            p = vbo["properties"]
            out["woonoppervlak"] = p.get("oppervlakte")
            out["gebruiksdoel"] = p.get("gebruiksdoel", "")
            out["pand_id"] = str(p.get("pandidentificatie", "") or "")
    except Exception:
        pass
    try:
        panden = _wfs(BAG_WFS, "bag:pand", bbox, count=10)
        pand = None
        if out.get("pand_id"):
            pand = next((f for f in panden
                         if str(f["properties"].get("identificatie")) == out["pand_id"]), None)
        if pand is None:
            pand = next((f for f in panden if _point_in_geom(x, y, f.get("geometry"))), None)
        if pand is None and panden:
            pand = panden[0]
        if pand:
            out["bouwjaar"] = pand["properties"].get("bouwjaar")
            out["footprint_m2"] = round(_polygon_area(pand.get("geometry")))
    except Exception:
        pass
    return out


def _perceel(x: float, y: float) -> dict:
    try:
        percelen = _wfs(KAD_WFS, "kadastralekaart:Perceel", (x - 2, y - 2, x + 2, y + 2), count=10)
        hit = next((f for f in percelen if _point_in_geom(x, y, f.get("geometry"))), None)
        hit = hit or (percelen[0] if percelen else None)
        if hit:
            p = hit["properties"]
            return {
                "perceel_m2": p.get("kadastraleGrootteWaarde"),
                "kadastraal": f"{p.get('kadastraleGemeenteWaarde', '')} "
                              f"{p.get('sectie', '')} {p.get('perceelnummer', '')}".strip(),
            }
    except Exception:
        pass
    return {}


def _cbs_buurt(buurtcode: str) -> dict:
    try:
        data = _get_json(CBS_ODATA, {
            "$filter": f"WijkenEnBuurten eq '{buurtcode}'", "$top": 1})
        rows = data.get("value", [])
        if not rows:
            return {}
        r = rows[0]
        def _f(*keys):
            for k in r:
                if any(k.startswith(p) for p in keys):
                    return r[k]
            return None
        return {
            "inwoners": _f("AantalInwoners"),
            "huishoudens_met_kinderen": _f("HuishoudensMetKinderen"),
            "gem_woz_x1000": _f("GemiddeldeWOZWaardeVanWoningen"),
            "pct_koopwoningen": _f("Koopwoningen"),
            "bevolkingsdichtheid": _f("Bevolkingsdichtheid"),
        }
    except Exception:
        return {}


def _vergunningvrij(perceel_m2: float | None, footprint_m2: float | None) -> dict:
    """Ruwe Bbl-indicatie. Bebouwingsgebied wordt benaderd als perceel minus
    hoofdgebouw; de werkelijke berekening vergt de erfgrens/voorgevellijn."""
    if not perceel_m2 or not footprint_m2 or perceel_m2 <= footprint_m2:
        return {}
    gebied = perceel_m2 - footprint_m2
    if gebied <= 100:
        toegestaan = gebied * 0.5
    else:
        toegestaan = 50 + (gebied - 100) * 0.2
    return {
        "bebouwingsgebied_m2": round(gebied),
        "vergunningvrij_bijbouw_m2": round(min(toegestaan, 150)),
        "aanbouw_regel": "aanbouw tot 4 m diep vanaf oorspronkelijke achtergevel is vergunningvrij (Bbl)",
    }


def _waarde_indicatie(woonopp, extra_m2, city_m2: dict | None) -> dict:
    if not city_m2 or not city_m2.get("median"):
        return {}
    m2p = city_m2["median"]
    out = {
        "stad_eur_m2_mediaan": round(m2p),
        "indicatie_huidige_waarde": round(woonopp * m2p) if woonopp else None,
    }
    if extra_m2:
        opbrengst = extra_m2 * m2p * WAARDEFACTOR_AANBOUW
        kosten = extra_m2 * BOUWKOSTEN_M2
        out["uitbreiding_m2"] = extra_m2
        out["uitbreiding_opbrengst"] = round(opbrengst)
        out["uitbreiding_bouwkosten"] = round(kosten)
        out["uitbreiding_netto_potentie"] = round(opbrengst - kosten)
    return out


def city_m2_stats(city: str) -> dict | None:
    """Mediaan €/m² uit de eigen FlipRadar-database."""
    import statistics
    from .db import Listing, SessionLocal
    with SessionLocal() as s:
        vals = [l.price_m2 for l in
                s.query(Listing).filter(Listing.city.ilike(city),
                                        Listing.price_m2.isnot(None),
                                        Listing.price_m2 > 0).all()]
    if len(vals) < 3:
        return None
    return {"median": statistics.median(vals), "min": min(vals),
            "max": max(vals), "n": len(vals)}


# ── hoofdfunctie ─────────────────────────────────────────────────

def analyse(query: str) -> dict:
    q = _parse_funda_url(query.strip())
    adres = _lookup_address(q)
    if not adres:
        return {"error": f"Adres niet gevonden voor '{q}'. Probeer 'straat huisnummer plaats'."}

    x, y = adres["_x"], adres["_y"]
    bag = _bag_data(x, y, str(adres.get("adresseerbaarobject_id", "")))
    perceel = _perceel(x, y)
    cbs = _cbs_buurt(adres.get("buurtcode", ""))
    stats = city_m2_stats(adres.get("woonplaatsnaam", ""))

    vv = _vergunningvrij(perceel.get("perceel_m2"), bag.get("footprint_m2"))
    # aanbouw-potentie: conservatief de helft van wat vergunningvrij mag,
    # gemaximeerd op 30 m2 (typische serre/aanbouw)
    extra = min(round((vv.get("vergunningvrij_bijbouw_m2") or 0) * 0.5), 30) or None
    waarde = _waarde_indicatie(bag.get("woonoppervlak"), extra, stats)

    return {
        "adres": adres.get("weergavenaam"),
        "stad": adres.get("woonplaatsnaam"),
        "wijk": adres.get("wijknaam"),
        "buurt": adres.get("buurtnaam"),
        "object": {
            "woonoppervlak_m2": bag.get("woonoppervlak"),
            "bouwjaar": bag.get("bouwjaar"),
            "gebruiksdoel": bag.get("gebruiksdoel"),
            "footprint_m2": bag.get("footprint_m2"),
            "perceel_m2": perceel.get("perceel_m2"),
            "kadastraal": perceel.get("kadastraal"),
        },
        "buurt_cbs": cbs,
        "vergunningvrij": vv,
        "waarde": waarde,
        "disclaimer": "Indicaties op basis van open data (BAG, Kadastrale kaart, CBS) "
                      "en eigen marktdata — geen taxatie. Bbl-berekening is een "
                      "benadering; het omgevingsplan van de gemeente is leidend.",
    }
