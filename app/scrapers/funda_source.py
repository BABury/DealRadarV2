"""Funda-bron via pyfunda (mobiele API) — actief aanbod + verkochte objecten."""
from __future__ import annotations

import datetime as dt
import os
import random
import time

from ..keywords import analyse_description

# Regio Eindhoven — de investeerder kijkt hier; het scrapebudget gaat dus
# volledig naar deze gemeenten. Andere steden: zet FUNDA_CITIES als env-var.
DEFAULT_CITIES = [
    "eindhoven", "veldhoven", "nuenen", "waalre", "best", "geldrop",
    "mierlo", "son en breugel", "helmond", "valkenswaard", "eersel", "oirschot",
]


def _known_urls() -> set[str]:
    """Alle Funda-urls die al in de database staan — zodat het detail-budget
    naar NIEUWE objecten gaat in plaats van elke dag dezelfde eerste N."""
    try:
        from ..db import Listing, SessionLocal
        with SessionLocal() as s:
            return {u for (u,) in s.query(Listing.url)
                    .filter(Listing.source == "funda") if u}
    except Exception:
        return set()


def _light_update(listing) -> dict | None:
    """Prijs-update voor een al bekend object, zonder detail-call.
    Houdt de prijshistorie (en dus het prijsverlaging-signaal) levend."""
    try:
        url = listing.url or ""
        price = listing.price.amount or None
        living = listing.areas.living or None
        if not url or not price:
            return None
        return {
            "url": url,
            "price": float(price),
            "living_area": float(living) if living else None,
            "price_m2": round(price / living) if price and living else None,
        }
    except Exception:
        return None


def _cities() -> list[str]:
    raw = os.getenv("FUNDA_CITIES", "")
    return [c.strip().lower() for c in raw.split(",") if c.strip()] or DEFAULT_CITIES


def scrape_funda(sink=None, on_total=None) -> list[dict]:
    try:
        from funda import Funda
    except ImportError as e:
        raise RuntimeError("pyfunda niet geinstalleerd") from e

    proxy = os.getenv("SCRAPER_PROXY", "")
    if proxy:
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[k] = proxy
        print("[funda] scraper-proxy actief", flush=True)

    max_price = int(os.getenv("FUNDA_MAX_PRICE", "2000000"))
    max_price_m2 = int(os.getenv("FUNDA_MAX_PRICE_M2", "6000"))
    max_per_city = int(os.getenv("FUNDA_MAX_PER_CITY", "100000"))
    delay = float(os.getenv("FUNDA_DETAIL_DELAY", "2.0"))

    cities = _cities()
    if on_total:
        on_total(len(cities))

    known = _known_urls()
    print(f"[funda] {len(known)} objecten al bekend in database", flush=True)

    items: list[dict] = []
    for city in cities:
        city_items: list[dict] = []
        status = "ok"
        skipped = 0
        already = 0
        try:
            print(f"[funda] start {city}...", flush=True)
            with Funda() as client:
                results = list(client.iter_search(city, max_price=max_price))
                print(f"[funda] {city}: {len(results)} gevonden "
                      f"(max EUR{max_price} / EUR{max_price_m2} per m2)", flush=True)
                done = 0
                for listing in results:
                    # Al bekend? Alleen prijs bijwerken (geen detail-call, telt
                    # niet mee voor het budget) — zo gaat het budget naar NIEUW.
                    lu = getattr(listing, "url", "") or ""
                    if lu and lu in known:
                        upd = _light_update(listing)
                        if upd:
                            city_items.append(upd)
                            already += 1
                        continue
                    if done >= max_per_city:
                        continue
                    pm2 = _search_price_m2(listing)
                    if pm2 is not None and pm2 > max_price_m2:
                        skipped += 1
                        continue
                    try:
                        full = client.listing(listing.id) if listing.id else listing
                    except Exception:
                        full = listing
                    time.sleep(random.uniform(delay * 0.75, delay * 1.5))
                    d = _to_dict(full or listing)
                    if not d:
                        continue
                    if d.get("price_m2") and d["price_m2"] > max_price_m2:
                        skipped += 1
                        continue
                    city_items.append(d)
                    known.add(d.get("url", ""))
                    done += 1
        except Exception as e:
            status = "blocked" if "403" in str(e) else "error"
            print(f"[funda] {city} MISLUKT: {e}", flush=True)
            if sink:
                try:
                    sink(city, [], status)
                except Exception:
                    pass
            time.sleep(5)
            continue

        items.extend(city_items)
        print(f"[funda] {city} klaar — {len(city_items)} objecten "
              f"({already} bekend/prijs-update, {skipped} overgeslagen op prijs/m2) "
              f"— totaal {len(items)}", flush=True)
        if sink:
            try:
                sink(city, city_items, status)
            except Exception:
                pass
        time.sleep(random.uniform(4, 9))
    return items


def scrape_funda_sold(sink=None, on_total=None) -> list[dict]:
    """Verkochte objecten per gemeente — ALLEEN zoekresultaten (goedkoop, geen
    detail-calls). Dit voedt de wijk/stad × segment-benchmarks.

    Funda toont bij 'verkocht' de laatste vraagprijs (niet de Kadaster-
    transactieprijs); binnen een wijk is dat een consistente en dus bruikbare
    benchmark. Sortering 'newest' = publicatiedatum aflopend, dus we kunnen
    stoppen zodra we voorbij de recency-horizon zijn."""
    try:
        from funda import Funda
    except ImportError as e:
        raise RuntimeError("pyfunda niet geinstalleerd") from e

    proxy = os.getenv("SCRAPER_PROXY", "")
    if proxy:
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[k] = proxy

    months = int(os.getenv("BENCHMARK_MONTHS", "12"))
    # Buffer: publicatiedatum ligt vóór verkoopdatum, dus horizon iets ruimer
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=int((months + 4) * 30.5))
    max_pages = int(os.getenv("SOLD_MAX_PAGES_PER_CITY", "400"))
    page_delay = float(os.getenv("SOLD_PAGE_DELAY", "1.5"))

    cities = _cities()
    if on_total:
        on_total(len(cities))

    items: list[dict] = []
    for city in cities:
        city_items: list[dict] = []
        status = "ok"
        try:
            print(f"[funda-verkocht] start {city}...", flush=True)
            with Funda() as client:
                seen_on_page = 0
                too_old = False
                for listing in client.iter_search(
                        city, category="sold", sort="newest", max_pages=max_pages):
                    seen_on_page += 1
                    if seen_on_page % 15 == 0:  # pauze per resultaatpagina
                        time.sleep(random.uniform(page_delay * 0.7, page_delay * 1.4))
                    d = _sold_to_dict(listing)
                    if not d:
                        continue
                    pub = _parse_date(d.get("publication_date"))
                    if pub and pub < cutoff:
                        too_old = True
                        break
                    city_items.append(d)
                if too_old:
                    print(f"[funda-verkocht] {city}: horizon bereikt "
                          f"(ouder dan {months + 4} mnd)", flush=True)
        except Exception as e:
            status = "blocked" if "403" in str(e) else "error"
            print(f"[funda-verkocht] {city} MISLUKT: {e}", flush=True)
            if sink:
                try:
                    sink(city, [], status)
                except Exception:
                    pass
            time.sleep(5)
            continue

        items.extend(city_items)
        print(f"[funda-verkocht] {city} klaar — {len(city_items)} verkochte objecten "
              f"— totaal {len(items)}", flush=True)
        if sink:
            try:
                sink(city, city_items, status)
            except Exception:
                pass
        time.sleep(random.uniform(4, 9))
    return items


def _parse_date(raw) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _sold_to_dict(listing) -> dict | None:
    try:
        addr = listing.address
        price = listing.price.amount or None
        living = listing.areas.living or None
        if not price or not living:
            return None
        pm2 = price / living
        if pm2 < 400 or pm2 > 30000 or living < 15:  # ruis eruit
            return None
        return {
            "url": listing.url or "",
            "address": addr.title or "",
            "city": (addr.city or "").strip(),
            "neighbourhood": (addr.neighbourhood or "").strip(),
            "postcode": (addr.postcode or "").strip(),
            "price": float(price),
            "living_area": float(living),
            "plot_area": float(listing.areas.plot) if listing.areas.plot else None,
            "price_m2": round(pm2),
            "property_type": listing.property_details.object_type or "",
            "publication_date": str(listing.publication_date or ""),
        }
    except Exception:
        return None


def _search_price_m2(listing):
    try:
        price = listing.price.amount
        living = listing.areas.living
        if price and living:
            return price / living
    except Exception:
        pass
    return None


def _to_dict(listing):
    try:
        addr = listing.address
        price = listing.price.amount or None
        living = listing.areas.living or None
        desc = listing.description or ""
        analysed = analyse_description(desc)
        vve = ""
        for label in ("Servicekosten", "VvE bijdrage", "Bijdrage VvE"):
            v = listing.characteristic(label)
            if v:
                vve = str(v)
                break
        photo = ""
        try:
            urls = getattr(listing, "photo_urls", None) or ()
            photo = urls[0] if urls else (getattr(listing, "thumbnail_url", "") or "")
        except Exception:
            pass
        pd = listing.property_details
        return {
            "photo_url": str(photo)[:600],
            "source": "funda",
            "url": listing.url or "",
            "address": addr.title or "",
            "city": addr.city or "",
            "neighbourhood": addr.neighbourhood or "",
            "postcode": addr.postcode or "",
            "province": addr.province or "",
            "price": float(price) if price else None,
            "living_area": float(living) if living else None,
            "plot_area": float(listing.areas.plot) if listing.areas.plot else None,
            "price_m2": round(price / living) if price and living else None,
            "property_type": pd.house_type or pd.object_type or "",
            "build_year": int(pd.construction_year) if pd.construction_year else None,
            "rooms": listing.rooms.total or None,
            "energy_label": listing.energy_label or "",
            "vve_monthly": vve,
            "broker": listing.broker.name if listing.broker else "",
            **analysed,
        }
    except Exception:
        return None
