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
        self.min_delay = float(os.getenv("FUNDA_DETAIL_DELAY", "4.0"))   # rustig tempo: Funda blokkeert op snelheid
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


def _rotatie(cities: list[str]) -> list[str]:
    """Per run maar een paar steden, en elke run de volgende.

    Funda blokkeert niet op IP maar op tempo: na ±200 pagina's in een half uur
    ging de deur dicht (eerste cloud-run, 14 sept). Door steeds FUNDA_CITIES_PER_RUN
    steden te doen en meerdere runs per dag te plannen blijven we eronder, en
    komt toch elke stad regelmatig langs. De positie volgt uit het aantal eerdere
    Funda-runs in de database, dus dit overleeft herstarts."""
    per_run = int(os.getenv("FUNDA_CITIES_PER_RUN", "4"))
    if per_run <= 0 or per_run >= len(cities):
        return cities
    try:
        from ..db import ScrapeRun, SessionLocal
        with SessionLocal() as s:
            n = s.query(ScrapeRun).filter(ScrapeRun.source == "funda").count()
    except Exception:
        n = 0
    start = (n * per_run) % len(cities)
    keuze = (cities + cities)[start:start + per_run]
    print(f"[funda-browser] rotatie: run {n + 1}, steden {start + 1}-{start + per_run} "
          f"van {len(cities)}: {', '.join(keuze)}", flush=True)
    return keuze


# JS dat op een ZOEKpagina elk woningkaartje uitleest (15 per pagina).
# Per unieke detail-link: het grootste omsluitende element dat alleen díe
# woning bevat en zowel een prijs als m² toont.
CARDS_JS = """() => {
  const uniq = el => new Set([...el.querySelectorAll('a[href*="/detail/koop/"]')]
                    .map(a => a.getAttribute('href').split('?')[0])).size;
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href*="/detail/koop/"]')) {
    const href = a.getAttribute('href').split('?')[0];
    if (seen.has(href)) continue;
    let el = a, best = null;
    for (let i = 0; i < 10 && el; i++) {
      if (uniq(el) > 1) break;
      const t = el.innerText || '';
      if (/€/.test(t) && /m²/.test(t)) best = el;
      el = el.parentElement;
    }
    if (!best) continue;
    seen.add(href);
    out.push({href, tekst: best.innerText});
  }
  const m = (document.body.innerText.match(/([\\d.]+)\\s*koopwoningen/) || [])[1] || null;
  return {kaarten: out, totaal: m};
}"""

_PC_RE = re.compile(r"^(\d{4}\s?[A-Z]{2})\s+(.+)$")
_LABEL_RE = re.compile(r"^(A\+{0,4}|[B-G])$")


def parse_card(href: str, tekst: str) -> dict | None:
    """Woningkaartje -> basisgegevens. Geen detailpagina nodig.
    Kaartje-opbouw (sept 2026): … | € 485.000 k.k. | Obrechtlaan 6 |
    5654 GH Eindhoven | 119 m² | 147 m² | 4 | A++ | slogan | makelaar"""
    delen = [d.strip() for d in (tekst or "").split("\n") if d.strip()]
    i = next((k for k, d in enumerate(delen) if d.startswith("€")), None)
    if i is None:
        return None
    prijs = _euro(delen[i])
    if not prijs:
        return None                      # 'prijs op aanvraag' e.d.
    adres = delen[i + 1] if i + 1 < len(delen) else ""
    postcode, plaats = "", ""
    if i + 2 < len(delen):
        m = _PC_RE.match(delen[i + 2])
        if m:
            postcode, plaats = m.group(1), m.group(2)
    m2s = [_m2(d) for d in delen[i + 2:] if d.endswith("m²")]
    wonen = m2s[0] if m2s else None
    perceel = m2s[1] if len(m2s) > 1 else None
    label = next((d for d in delen[i + 2:] if _LABEL_RE.match(d)), "")
    if not wonen:
        return None
    url = (BASE + href) if href.startswith("/") else href
    soort = "Appartement" if "/appartement-" in url else "Woonhuis"
    if not plaats:
        m = re.search(r"/detail/koop/([^/]+)/", url)
        plaats = m.group(1).replace("-", " ").title() if m else ""
    return {
        "source": "funda", "url": url.split("?")[0], "address": adres[:300],
        "city": plaats[:100], "postcode": postcode,
        "price": prijs, "living_area": wonen, "plot_area": perceel,
        "price_m2": round(prijs / wonen), "property_type": soort,
        "energy_label": label,
    }


def scrape_funda_browser(sink=None, on_total=None) -> list[dict]:
    """Scrape woningaanbod per stad via de browser — in twee trappen.

    1. ZOEKPAGINA'S: elk kaartje (15 per pagina) levert prijs, m², perceel,
       adres en label. Zo ziet één pagina 15 huizen i.p.v. 1 → veel meer
       dekking binnen Funda's tempogrens (±200 pagina's per half uur).
       Bekende huizen krijgen een prijs-update (→ prijsverlaging-signaal).
    2. DETAILPAGINA'S alleen voor de veelbelovendste nieuwe huizen (laagste
       €/m² eerst): bouwjaar, woonlagen en omschrijving (splits-/verbouwtaal).
    Alles telt mee in één paginabudget per run."""
    max_price = int(os.getenv("FUNDA_MAX_PRICE", "10000000"))   # geen plafond (splitsdoel)
    max_pages = int(os.getenv("FUNDA_MAX_PAGES", "40"))          # zoekpagina's per stad
    budget = int(os.getenv("FUNDA_PAGE_BUDGET", "110"))          # pagina's per run (alle steden)
    max_details = int(os.getenv("FUNDA_MAX_DETAILS", "6"))       # detailpagina's per stad
    stad_timeout = float(os.getenv("SCRAPE_CITY_TIMEOUT", "900"))

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
    cities = _rotatie(cities)
    if on_total:
        on_total(len(cities))

    alles: list[dict] = []
    geblokkeerd_op_rij = 0
    gebruikt = 0
    with Browser() as br:
        for n_stad, city in enumerate(cities):
            if geblokkeerd_op_rij >= 2 or gebruikt >= budget:
                reden = ("Funda blokkeert dit IP" if geblokkeerd_op_rij >= 2
                         else f"paginabudget ({budget}) op")
                print(f"[funda-browser] {reden} — {city} overgeslagen", flush=True)
                if sink:
                    try:
                        sink(city, [], "blocked" if geblokkeerd_op_rij >= 2 else "skipped")
                    except Exception:
                        pass
                continue
            # eerlijk verdelen: elke resterende stad een gelijk deel van het budget
            stad_budget = max(3, (budget - gebruikt) // (len(cities) - n_stad))
            deadline = time.time() + stad_timeout
            per_url: dict[str, dict] = {}
            nieuwe: list[dict] = []
            status, pagina, totaal = "ok", 0, None
            try:
                # ── trap 1: zoekpagina's (15 huizen per pagina) ──
                zoek_budget = max(1, stad_budget - max_details)
                for p in range(1, max_pages + 1):
                    if pagina >= zoek_budget or time.time() > deadline:
                        break
                    _, data = br.open(search_url(city, max_price, p), evaluate=CARDS_JS)
                    pagina += 1
                    if data is None:
                        status = "blocked"
                        break
                    if p == 1:
                        totaal = data.get("totaal")
                    kaarten = [parse_card(k["href"], k["tekst"]) for k in data.get("kaarten") or []]
                    kaarten = [k for k in kaarten if k and k["url"] not in per_url]
                    if not kaarten:
                        break
                    for k in kaarten:
                        if k["url"] in bekend:
                            # alleen prijs/m² bijwerken -> houdt prijshistorie levend
                            per_url[k["url"]] = {**{x: k[x] for x in
                                                    ("url", "price", "living_area", "price_m2")},
                                                 "_update_only": True}
                        else:
                            per_url[k["url"]] = k
                            nieuwe.append(k)

                # ── trap 2: detail voor de veelbelovendste nieuwe huizen ──
                nieuwe.sort(key=lambda k: k.get("price_m2") or 10 ** 9)
                for k in nieuwe[:max(0, stad_budget - pagina)]:
                    if time.time() > deadline or status == "blocked":
                        break
                    _, det = br.open(k["url"], evaluate=DETAIL_JS)
                    pagina += 1
                    if det:
                        d = parse_detail(det, k["url"])
                        if d:
                            per_url[k["url"]] = {**k, **{a: b for a, b in d.items() if b not in (None, "")}}
            except Exception as e:
                status = "error"
                print(f"[funda-browser] {city} fout: {str(e)[:120]}", flush=True)

            gebruikt += pagina
            stad_items = list(per_url.values())
            bekend.update(per_url)
            geblokkeerd_op_rij = geblokkeerd_op_rij + 1 if status == "blocked" else 0
            alles.extend(stad_items)
            print(f"[funda-browser] {city}: Funda meldt {totaal or '?'} woningen · "
                  f"{len(stad_items)} gezien ({len(nieuwe)} nieuw) in {pagina} pagina's "
                  f"· budget {gebruikt}/{budget}", flush=True)
            if sink:
                try:
                    sink(city, stad_items, status)
                except Exception as e:
                    # Niet stil laten mislukken: dan verdwijnt de oogst zonder spoor.
                    import traceback
                    print(f"[funda-browser] {city}: OPSLAAN MISLUKT: {e}\n"
                          f"{traceback.format_exc()[:600]}", flush=True)
    return alles
