"""Veilingplatform-scrapers, gebaseerd op de echte site-structuren (juli 2026).

- veilingnotaris.nl en bog-auctions.com draaien op hetzelfde platform
  (veilingportaal): server-side HTML met a.c-thumb kaarten en
  /veilingen/{id}/{slug}/ links.
- biedboek.nl heeft een open JSON-API: /api/real-estate?language=nl

Endpoints zijn via env vars te overriden zonder redeploy:
  VEILINGNOTARIS_URL, BOG_AUCTIONS_URL, BIEDBOEK_API
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..keywords import analyse_description, parse_price

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Cache-Control": "max-age=0",
}
TIMEOUT = 30

# Veilingen met deze status zijn al afgerond — geen kans meer
_CLOSED_STATUSES = {"gegund", "vervallen", "ingetrokken", "onderhands verkocht", "niet gegund"}

_DATE_RE = re.compile(
    r"\b(?:ma|di|wo|do|vr|za|zo)?\s*(\d{1,2}\s+(?:jan|feb|mrt|apr|mei|jun|jul|aug|sep|okt|nov|dec)\w*\s+\d{4}(?:\s+\d{1,2}:\d{2})?)",
    re.IGNORECASE,
)


def _to_float(v) -> float | None:
    """'41.51.78' (hectare-notatie), '1234,5' of 350 -> float of None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v else None
    txt = str(v).strip().replace(",", ".")
    if txt.count(".") > 1:  # hectare-notatie ha.are.ca -> niet betrouwbaar
        return None
    try:
        return float(txt) or None
    except ValueError:
        return None


def _get(url: str) -> requests.Response:
    proxy = os.getenv("SCRAPER_PROXY", "")
    kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ── veilingportaal-platform (veilingnotaris + bog-auctions) ──────

def _scrape_veilingportaal(list_url: str, source: str) -> list[dict]:
    r = _get(list_url)
    soup = BeautifulSoup(r.text, "html.parser")
    origin = f"{urlparse(list_url).scheme}://{urlparse(list_url).netloc}"

    items: list[dict] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='/veilingen/']"):
        href = a.get("href", "")
        if not re.search(r"/veilingen/\d+", href):
            continue
        url = href if href.startswith("http") else origin + href
        if url in seen:
            continue
        seen.add(url)

        title_el = a.select_one(".c-thumb__title, h3")
        city_el = a.select_one(".c-thumb__subtitle, h5")
        title = title_el.get_text(strip=True) if title_el else ""
        city = city_el.get_text(strip=True) if city_el else ""
        text = a.get_text(" ", strip=True)
        lower = text.lower()

        # sla afgeronde veilingen over
        if any(s in lower[:60] for s in _CLOSED_STATUSES):
            continue
        if not title:
            title = text[:120]

        auction_type = ""
        prop_type = ""
        lis = [li.get_text(strip=True) for li in a.select("li")]
        if lis:
            auction_type = lis[0]
            if len(lis) > 1:
                prop_type = lis[1]

        m = _DATE_RE.search(text)
        items.append({
            "source": source,
            "url": url,
            "address": title[:200],
            "city": city[:80],
            "property_type": prop_type[:100] or auction_type[:100],
            "price": parse_price(text) if "€" in text else None,
            "auction_date": m.group(1)[:40] if m else "",
            **analyse_description(f"{title} {auction_type} {prop_type}"),
        })

    if not items:
        raise RuntimeError(f"Geen veilingen geparsed op {list_url} — "
                           "structuur gewijzigd of verzoek geblokkeerd.")
    return _verrijk(items, source)


def _verrijk(items: list[dict], source: str) -> list[dict]:
    """Vult m², bouwjaar, omschrijving, executie/huurbeding aan.

    De overzichtspagina geeft alleen titel en type. Deze sites horen bij één
    netwerk met gedeelde veilingnummers; vastgoedveiling.nl levert per nummer
    de volledige JSON. Zonder deze stap had elk object 0 m² — en kon het model
    er niets mee (ook niet in de oude DealRadar)."""
    import time
    from .vastgoedveiling import _auction_to_dict, fetch_auction

    uit: list[dict] = []
    verrijkt = 0
    for it in items:
        m = re.search(r"/veilingen?/(\d+)", it["url"])
        a = fetch_auction(m.group(1)) if m else None
        if a:
            d = _auction_to_dict(a, it["url"])
            if d is None:          # buitenlandse veiling -> overslaan
                continue
            d["source"] = source
            d["auction_date"] = d.get("auction_date") or it.get("auction_date", "")
            uit.append(d)
            verrijkt += 1
            time.sleep(0.3)
        else:
            uit.append(it)         # geen detaildata: houd de lijst-info
    print(f"[{source}] {verrijkt}/{len(items)} objecten verrijkt met m²/bouwjaar", flush=True)
    return uit


def _try_urls(urls: list[str], source: str) -> list[dict]:
    last_err: Exception | None = None
    for u in urls:
        try:
            return _scrape_veilingportaal(u, source)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f"geen werkende URL voor {source}")


def scrape_veilingnotaris() -> list[dict]:
    env = os.getenv("VEILINGNOTARIS_URL", "")
    urls = [env] if env else [
        "https://veilingnotaris.nl/veiling",
        "https://veilingnotaris.nl/veilingen/",
        "https://www.veilingnotaris.nl/veiling",
    ]
    return _try_urls(urls, "veilingnotaris")


def scrape_bog_auctions() -> list[dict]:
    env = os.getenv("BOG_AUCTIONS_URL", "")
    urls = [env] if env else [
        "https://bog-auctions.com/veilingen/",
        "https://bog-auctions.com/auctions/upcoming",
        "https://www.bog-auctions.com/auctions/upcoming",
    ]
    return _try_urls(urls, "bog_auctions")


# ── biedboek.nl — open JSON-API van het Rijksvastgoedbedrijf ─────

def scrape_biedboek() -> list[dict]:
    url = os.getenv("BIEDBOEK_API", "https://www.biedboek.nl/api/real-estate?language=nl")
    r = _get(url)
    rows = r.json()
    if not isinstance(rows, list):
        rows = rows.get("results") or rows.get("items") or rows.get("data") or []

    items: list[dict] = []
    only_nl = os.getenv("BIEDBOEK_ONLY_NL", "1") == "1"
    for row in rows:
        if not isinstance(row, dict) or row.get("isArchived"):
            continue
        country = ((row.get("country") or {}).get("code") or "").upper()
        if only_nl and ((country and country != "NLD") or row.get("caribbeanIsland")):
            continue
        title = (row.get("title") or "").strip()
        short = row.get("shortId") or row.get("id") or ""
        if not title or not short:
            continue

        price = _to_float(row.get("askingPrice")) or _to_float(row.get("price"))
        if price is not None and price < 1000:
            price = None  # 0.0 = geen vraagprijs bekend

        surface = _to_float(row.get("surface"))
        parcel = _to_float(row.get("parcelSize"))
        year = row.get("yearConstructed") or None
        bidding = row.get("biddingData") or {}
        end_date = str(bidding.get("endDate") or "")[:10]

        items.append({
            "source": "biedboek",
            "url": f"https://www.biedboek.nl/nl/realestate/{short}",
            "address": title[:200],
            "city": (row.get("city") or "")[:80],
            "price": price,
            "living_area": surface,
            "plot_area": parcel,
            "price_m2": round(price / surface) if price and surface else None,
            "build_year": int(year) if year and str(year).isdigit() and int(year) > 1500 else None,
            "auction_date": end_date,
            **analyse_description(title),
        })

    if not items:
        raise RuntimeError("Biedboek-API gaf geen bruikbare objecten terug — "
                           "check BIEDBOEK_API env var.")
    return items
