"""Funda via een echte browser (Playwright) — vervangt de geblokkeerde API.

Waarom: Funda heeft de mobiele API die pyfunda gebruikt dichtgezet (HTTP/2
stream reset op elke aanroep), terwijl funda.nl in een gewone browser gewoon
werkt. Dezelfde aanpak als de funda-in-business scraper: een echte Chromium,
buiten beeld, met menselijke pauzes. Er worden GEEN captcha's omzeild — bij een
blokkade wacht het script en slaat de stad over.

Levert dezelfde dict-vorm als de oude funda_source, dus scoring, filter en
opslag werken ongewijzigd.
"""
from __future__ import annotations

import os
import random
import re
import time
import urllib.parse

from ..keywords import analyse_description

BASE = "https://www.funda.nl"
BLOCK_MARKERS = ("je bent bijna op de pagina", "verifiëren dat onze bezoekers",
                 "recaptcha", "toegang geweigerd")


def _cities() -> list[str]:
    raw = os.getenv("FUNDA_CITIES", "")
    return [c.strip().lower() for c in raw.split(",") if c.strip()] or [
        "amsterdam", "rotterdam", "den-haag", "utrecht", "eindhoven", "haarlem",
        "leiden", "delft", "groningen", "nijmegen", "arnhem", "zwolle"]


def search_url(city: str, max_price: int, page: int = 1, sort_new: bool = True) -> str:
    # Splitsstrategie: standaard alleen HUIZEN (een appartement splits je niet)
    # en pas vanaf een oppervlak waar 2 appartementen in passen.
    types = [t.strip() for t in os.getenv("FUNDA_OBJECT_TYPES", "house").split(",") if t.strip()]
    min_m2 = int(os.getenv("FUNDA_MIN_M2", "110"))
    q = {
        "selected_area": f'["{_slug(city)}"]',
        "price": f'"0-{max_price}"',
        "object_type": "[" + ",".join(f'"{t}"' for t in types) + "]",
    }
    if min_m2 > 0:
        q["floor_area"] = f'"{min_m2}-"'
    if sort_new:
        q["sort"] = '"date_down"'
    if page > 1:
        q["search_result"] = str(page)
    return f"{BASE}/zoeken/koop?" + urllib.parse.urlencode(q)


# ── parsers ───────────────────────────────────────────────────────────
def _euro(text: str) -> float | None:
    m = re.search(r"€\s?([\d.]{4,})", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _m2(text: str) -> float | None:
    m = re.search(r"([\d.]+)\s*m²", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _int(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def parse_detail(data: dict, url: str) -> dict | None:
    """data = {'kenmerken': {...}, 'h1': str, 'omschrijving': str}"""
    k = data.get("kenmerken") or {}
    prijs = _euro(k.get("Vraagprijs") or k.get("Koopprijs") or "")
    wonen = _m2(k.get("Wonen") or k.get("Woonoppervlakte") or "")
    if not prijs or not wonen:
        return None

    perceel = _m2(k.get("Perceel") or k.get("Oppervlakte") or "")
    bouwjaar = _int(k.get("Bouwjaar") or "")
    if bouwjaar and not (1500 < bouwjaar < 2100):
        bouwjaar = None

    eig = (k.get("Eigendomssituatie") or "").lower()
    omschrijving = data.get("omschrijving") or ""
    analysed = analyse_description(omschrijving)
    analysed["erfpacht"] = "erfpacht" in eig or analysed.get("erfpacht", False)

    # plaats uit de URL: /detail/koop/{plaats}/{slug}/{id}/
    m = re.search(r"/detail/koop/([^/]+)/", url)
    plaats = (m.group(1).replace("-", " ").title() if m else "")

    adres = (data.get("h1") or "").split("\n")[0].strip()

    return {
        "source": "funda",
        "url": url.split("?")[0],
        "address": adres[:300],
        "city": plaats[:100],
        "postcode": (re.search(r"\b\d{4}\s?[A-Z]{2}\b", data.get("h1") or "") or [""])[0]
        if re.search(r"\b\d{4}\s?[A-Z]{2}\b", data.get("h1") or "") else "",
        "price": prijs,
        "living_area": wonen,
        "plot_area": perceel,
        "price_m2": round(prijs / wonen) if wonen else None,
        "property_type": (k.get("Soort woonhuis") or k.get("Soort appartement") or "")[:120],
        "build_year": bouwjaar,
        "rooms": _int(k.get("Aantal kamers") or ""),
        "floors": _int(k.get("Aantal woonlagen") or ""),
        "energy_label": (k.get("Energielabel") or "")[:8].strip(),
        "published": "",     # 'Aangeboden sinds' vereist inlog op Funda
        "photo_url": data.get("foto") or "",
        "broker": (data.get("makelaar") or "")[:200],
        **analysed,
    }


# JS dat op de detailpagina alles in één keer uitleest
DETAIL_JS = """() => {
  const kenmerken = {};
  document.querySelectorAll('dt').forEach(dt => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') {
      const k = dt.innerText.trim();
      const v = dd.innerText.trim().replace(/\\s+/g, ' ');
      if (k && v && k.length < 60) kenmerken[k] = v;
    }
  });
  let omschrijving = '';
  const cand = document.querySelector('[data-testid="listing-description"]')
            || document.querySelector('.object-description-body')
            || document.querySelector('main article');
  if (cand) omschrijving = cand.innerText;
  const img = document.querySelector('main img');
  const mk = document.querySelector('[data-testid="listing-broker"], a[href*="/makelaar/"]');
  return {
    kenmerken,
    h1: (document.querySelector('h1') || {}).innerText || '',
    omschrijving: (omschrijving || '').slice(0, 6000),
    foto: img ? img.src : '',
    makelaar: mk ? mk.innerText.trim() : ''
  };
}"""


class Browser:
    """Echte Chromium, buiten beeld — komt langs de botcheck zonder te storen."""

    def __init__(self):
        self._pw = self._browser = self._ctx = None
        self.min_delay = float(os.getenv("FUNDA_DETAIL_DELAY", "2.0"))
        self.max_delay = self.min_delay * 2

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        if os.getenv("FUNDA_WINDOW_OFFSCREEN", "1") != "0":
            args += ["--window-position=-32000,-32000", "--window-size=1440,900"]
        self._browser = self._pw.chromium.launch(headless=False, args=args)
        # Geen vaste user-agent: een 'headed' Chromium heeft een realistische eigen
        # UA die klopt met platform en versie (Mac lokaal, Linux op Railway).
        # Een UA die niet bij het platform past is juist een bot-signaal.
        self._ctx = self._browser.new_context(
            locale="nl-NL", timezone_id="Europe/Amsterdam",
            viewport={"width": 1440, "height": 900},
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        return self

    def __exit__(self, *exc):
        for closer in (self._ctx, self._browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def _pauze(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def open(self, url: str, evaluate: str | None = None, retries: int = 2):
        """Open een pagina; geeft (html, resultaat-van-evaluate) of (None, None)."""
        from playwright.sync_api import TimeoutError as PWTimeout
        page = self._ctx.new_page()
        try:
            for poging in range(retries + 1):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1400)
                    html = page.content()
                except PWTimeout:
                    time.sleep(5 * (poging + 1))
                    continue
                if any(m in html.lower() for m in BLOCK_MARKERS):
                    wacht = 20 * (poging + 1)
                    print(f"[funda-browser] botcheck — {wacht}s wachten", flush=True)
                    time.sleep(wacht)
                    continue
                data = page.evaluate(evaluate) if evaluate else None
                return html, data
            return None, None
        finally:
            page.close()
            self._pauze()


def diagnose(city: str = "eindhoven") -> dict:
    """Test: komt een echte browser vanaf DEZE machine langs Funda's botcheck?
    Eén zoekpagina, geen opslag. Bedoeld voor Railway (datacenter-IP)."""
    t0 = time.time()
    try:
        with Browser() as br:
            br.min_delay = br.max_delay = 0.1
            html, _ = br.open(search_url(city, 10_000_000), retries=0)
    except Exception as e:
        return {"ok": False, "fout": f"{type(e).__name__}: {str(e)[:200]}",
                "seconden": round(time.time() - t0, 1)}
    if html is None:
        return {"ok": False, "geblokkeerd": True, "seconden": round(time.time() - t0, 1),
                "uitleg": "Funda toont de botcheck aan dit IP — cloud-scrapen werkt hier niet."}
    links = set(re.findall(r'href="(/detail/koop/[^"]+)"', html))
    m = re.search(r"([\d.]+)\s*koopwoningen", html)
    return {"ok": bool(links), "geblokkeerd": False, "objecten_op_pagina": len(links),
            "funda_meldt": m.group(1) if m else None, "bytes": len(html),
            "seconden": round(time.time() - t0, 1)}


def _slug(city: str) -> str:
    """Funda-gebiedsnaam: 'Den Haag' -> 'den-haag'."""
    s = city.strip().lower().replace("'s-gravenhage", "den-haag")
    return re.sub(r"\s+", "-", s)


def _veiling_steden(bekende: list[str]) -> list[str]:
    """Steden van grote (splitsbare) veilingwoningen waar het model nog geen
    verkoopprijzen kent. Door daar Funda te scrapen krijgt het model een
    meetlat en kan het die veiling doorrekenen (max. bod)."""
    try:
        from ..db import Listing, SessionLocal
        from ..scenarios import city_medians_all
        medianen = city_medians_all()
        with SessionLocal() as s:
            rows = (s.query(Listing.city)
                    .filter(Listing.source.in_(("vastgoedveiling", "veilingnotaris",
                                                "bog_auctions")),
                            Listing.living_area >= 110).distinct().all())
    except Exception:
        return []
    have = {_slug(c) for c in bekende}
    extra = []
    for (c,) in rows:
        if not c or (c or "").lower() in medianen:
            continue
        sl = _slug(c)
        if sl not in have and sl not in extra:
            extra.append(sl)
    return extra[:int(os.getenv("FUNDA_EXTRA_CITIES_MAX", "12"))]


def scrape_funda_browser(sink=None, on_total=None) -> list[dict]:
    """Scrape actief woningaanbod per stad via de browser."""
    max_price = int(os.getenv("FUNDA_MAX_PRICE", "10000000"))   # geen plafond (splitsdoel)
    max_per_city = int(os.getenv("FUNDA_MAX_PER_CITY", "40"))
    max_pages = int(os.getenv("FUNDA_MAX_PAGES", "10"))
    stad_timeout = float(os.getenv("SCRAPE_CITY_TIMEOUT", "600"))

    from ..db import Listing, SessionLocal
    with SessionLocal() as s:
        bekend = {u for (u,) in s.query(Listing.url).filter(Listing.source == "funda") if u}
    print(f"[funda-browser] {len(bekend)} objecten al bekend", flush=True)

    cities = _cities()
    # Automatisch ook de steden van splitsbare veilingkandidaten zonder marktdata
    extra = _veiling_steden(cities)
    if extra:
        print(f"[funda-browser] + veilingsteden voor marktprijzen: {', '.join(extra)}", flush=True)
        cities = cities + extra
    if on_total:
        on_total(len(cities))

    alles: list[dict] = []
    geblokkeerd_op_rij = 0
    with Browser() as br:
        for city in cities:
            # Noodstop: blokkeert Funda dit IP (bv. een datacenter), dan niet
            # elke stad opnieuw proberen — dat kost alleen tijd.
            if geblokkeerd_op_rij >= 2:
                print(f"[funda-browser] Funda blokkeert dit IP — {city} en verder overgeslagen",
                      flush=True)
                if sink:
                    try:
                        sink(city, [], "blocked")
                    except Exception:
                        pass
                continue
            deadline = time.time() + stad_timeout
            stad_items: list[dict] = []
            status = "ok"
            try:
                # 1. object-URLs verzamelen
                urls: list[str] = []
                for p in range(1, max_pages + 1):
                    if time.time() > deadline or len(urls) >= max_per_city:
                        break
                    html, _ = br.open(search_url(city, max_price, p))
                    if html is None:
                        status = "blocked"
                        break
                    if p == 1:
                        # Laat zien hoeveel Funda er vindt mét filters — zo zie je
                        # in de log of prijs/oppervlak/type-filter zijn toegepast.
                        m = re.search(r"([\d.]+)\s*koopwoningen", html)
                        print(f"[funda-browser] {city}: Funda meldt "
                              f"{m.group(1) if m else '?'} woningen binnen de filters",
                              flush=True)
                    gevonden = re.findall(r'href="(/detail/koop/[^"]+)"', html)
                    nieuw = [BASE + u.split("?")[0] for u in dict.fromkeys(gevonden)]
                    nieuw = [u for u in nieuw if u not in urls]
                    if not nieuw:
                        break
                    urls.extend(nieuw)
                urls = [u for u in urls if u not in bekend][:max_per_city]
                print(f"[funda-browser] {city}: {len(urls)} nieuwe objecten", flush=True)

                # 2. detailpagina's ophalen
                for u in urls:
                    if time.time() > deadline:
                        print(f"[funda-browser] {city}: stad-timeout", flush=True)
                        break
                    _, data = br.open(u, evaluate=DETAIL_JS)
                    if not data:
                        continue
                    d = parse_detail(data, u)
                    if d:
                        stad_items.append(d)
                        bekend.add(d["url"])
            except Exception as e:
                status = "error"
                print(f"[funda-browser] {city} fout: {str(e)[:120]}", flush=True)

            geblokkeerd_op_rij = geblokkeerd_op_rij + 1 if status == "blocked" else 0
            alles.extend(stad_items)
            print(f"[funda-browser] {city} klaar — {len(stad_items)} objecten "
                  f"(totaal {len(alles)})", flush=True)
            if sink:
                try:
                    sink(city, stad_items, status)
                except Exception as e:
                    # Niet stil laten mislukken: dan verdwijnt de oogst zonder spoor.
                    import traceback
                    print(f"[funda-browser] {city}: OPSLAAN MISLUKT: {e}\n"
                          f"{traceback.format_exc()[:600]}", flush=True)
    return alles
