"""vastgoedveiling.nl — veilingpanden (gedwongen/vrijwillige verkoop).

Waarom deze bron: veiling = gemotiveerde verkoop, vaak onder marktprijs. De
site draait op Next.js en levert een compleet `__NEXT_DATA__`-JSON per object
(adres, startbod, oppervlakte, bouwjaar, objecttype). Geen bot-bescherming en
geen browser nodig — kale HTTP volstaat, dus dit werkt óók op Railway waar
Funda het datacenter-IP blokkeert.

De site labelt objecten zelf als 'Transformatieobject' / 'Herontwikkeling';
die typen zijn direct interessant voor woningtransformatie.
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

from ..keywords import analyse_description

BASE = "https://vastgoedveiling.nl"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Objecttypen die de site zelf als ontwikkel-/transformatiekans bestempelt.
TRANSFORM_TYPES = {"transformatieobject", "herontwikkeling",
                   "herontwikkelingslocatie", "voormalig politiebureau"}


def _get(url: str, timeout: int = 25) -> str:
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Accept-Language": "nl-NL,nl;q=0.9"},
                     timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


def _next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _to_int(v):
    """Getal uit een veld halen. Waarden komen soms als 'BAG: 379' binnen."""
    if v is None:
        return None
    m = re.search(r"\d[\d.]*", str(v))
    if not m:
        return None
    try:
        return int(m.group(0).replace(".", ""))
    except ValueError:
        return None


# 'startbod' is bij deze veilingen een vaste bovengrens van het opbod-mechanisme
# (vrijwel altijd 1.000.000), NIET de vraagprijs. Zo'n bedrag als prijs opslaan
# zou de P&L en de stadsmedianen vervuilen — dus we laten de prijs leeg en
# rekenen voor veilingen met een maximaal bod (zie scenarios.max_bid).
_PLACEHOLDER_BEDRAGEN = {1000000, 500000}


def _echte_prijs(a: dict) -> float | None:
    for veld in ("koopsom", "vraagprijs", "inzetsom", "gunningsbedrag"):
        v = _to_int(a.get(veld))
        if v and v not in _PLACEHOLDER_BEDRAGEN:
            return float(v)
    return None


def _auction_to_dict(a: dict, url: str) -> dict | None:
    # Alleen Nederland (de site bevat ook Duitse/Belgische veilingen)
    if str(a.get("land", "")).lower() not in ("nl", "") and \
            a.get("land_compleet", "") != "Nederland":
        return None

    opp = _to_int(a.get("oppervlakte_object")) or None
    perceel = _to_int(a.get("oppervlakte_perceel")) or None
    prijs = _echte_prijs(a)   # None = veilingprijs nog onbekend (normaal)

    bouwjaar = None
    if str(a.get("bouwjaar", "")).strip():
        bm = re.search(r"\d{4}", str(a["bouwjaar"]))
        bouwjaar = int(bm.group(0)) if bm else None

    beschrijving = a.get("kavelbeschrijving") or ""
    analysed = analyse_description(beschrijving)

    # Door de site zelf gelabelde ontwikkelkans telt als ontwikkelsignaal
    otype = str(a.get("object_type", "") or "")
    ocat = str(a.get("object_type_categorie", "") or "")
    transform = otype.lower() in TRANSFORM_TYPES or ocat.lower() in TRANSFORM_TYPES
    if transform:
        analysed["flag_ontwikkeling"] = True

    # Zonder maat én zonder ontwikkelsignaal valt er niets te rekenen of te
    # beoordelen — dan slaan we het object niet op.
    if not opp and not perceel and not analysed.get("flag_ontwikkeling"):
        return None

    straat = (a.get("straat") or "").strip()
    huisnr = (a.get("huisnummer") or "").strip()
    adres = f"{straat} {huisnr}".strip() or (a.get("name") or "")

    return {
        "source": "vastgoedveiling",
        "url": url,
        "address": adres,
        "city": (a.get("plaats") or "").strip(),
        "postcode": (a.get("postcode") or "").strip(),
        "province": (a.get("provincie") or "").strip(),
        "price": prijs,
        "living_area": float(opp) if opp else None,
        "plot_area": float(perceel) if perceel else None,
        "price_m2": round(prijs / opp) if (prijs and opp) else None,
        "property_type": otype,
        "build_year": bouwjaar,
        "auction_date": str(a.get("eindtijd") or a.get("starttijd") or ""),
        "published": str(a.get("publicatiedatum") or ""),
        "photo_url": str(a.get("thumb") or "")[:600],
        "broker": a.get("makelaar_naam") or "",
        "context": beschrijving[:2000],
        **analysed,
    }


def scrape_vastgoedveiling() -> list[dict]:
    max_details = int(os.getenv("VV_MAX_DETAILS", "200"))
    delay = float(os.getenv("VV_DELAY", "0.4"))

    try:
        lijst = _get(f"{BASE}/veilingen")
    except Exception as e:
        raise RuntimeError(f"vastgoedveiling lijst mislukt: {e}") from e

    urls, gezien = [], set()
    for vid, slug in re.findall(r"/veiling/(\d+)/([a-z0-9-]+)", lijst):
        if vid not in gezien:
            gezien.add(vid)
            urls.append(f"{BASE}/veiling/{vid}/{slug}")
    print(f"[vastgoedveiling] {len(urls)} veilingen gevonden", flush=True)

    items: list[dict] = []
    for url in urls[:max_details]:
        try:
            nd = _next_data(_get(url))
            a = (nd or {}).get("props", {}).get("pageProps", {}).get("auction")
        except Exception as e:
            print(f"[vastgoedveiling] fout bij {url}: {str(e)[:80]}", flush=True)
            continue
        if not a:
            continue
        d = _auction_to_dict(a, url)
        if d:
            items.append(d)
        time.sleep(delay)

    print(f"[vastgoedveiling] {len(items)} bruikbare objecten", flush=True)
    return items
