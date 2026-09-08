"""Snelle scan: alleen NIEUW aanbod, hoge frequentie.

Waarom apart van de nachtelijke scrape: goede deals zijn binnen 24-48 uur weg.
Een dagelijkse run om 06:00 ziet een object dat om 09:00 online komt pas de
volgende ochtend — 21 uur te laat.

Deze scan is bewust licht:
  • alleen de zoekresultaten (geen detail-calls) -> snel en zuinig
  • alleen objecten met een URL die nog niet in de database staat
  • daarna één detail-call per NIEUW object, zodat scoring compleet is
  • ten slotte: scoren + melden

Draait standaard elke QUICKSCAN_MINUTES minuten (default 20).
"""
from __future__ import annotations

import os
import random
import time

from ..db import log_run, upsert_listings
from ..scoring import compute_scores
from .funda_source import (_cities, _city_timeout, _client_params, _install_proxy,
                           _is_block, _known_urls, _to_dict)


def _max_new() -> int:
    """Bovengrens per scan: een uitschieter mag de scan niet laten uitlopen."""
    return int(os.getenv("QUICKSCAN_MAX_NEW", "25"))


def quick_scan(sink=None) -> dict:
    """Zoek nieuw aanbod en voeg het toe. Geeft een rapport terug."""
    try:
        from funda import Funda
    except ImportError as e:
        raise RuntimeError("pyfunda niet geinstalleerd") from e

    _install_proxy()
    max_price = int(os.getenv("FUNDA_MAX_PRICE", "2000000"))
    max_price_m2 = int(os.getenv("FUNDA_MAX_PRICE_M2", "6000"))
    max_new = _max_new()
    delay = float(os.getenv("FUNDA_DETAIL_DELAY", "2.0"))

    known = _known_urls()
    cities = _cities()
    nieuw: list[dict] = []
    geblokkeerd: list[str] = []

    for city in cities:
        if len(nieuw) >= max_new:
            break
        # Nieuwste eerst: dan staat verse aanbod bovenaan en kunnen we stoppen
        # zodra we bekende objecten tegenkomen.
        deadline = time.time() + _city_timeout()
        try:
            with Funda(**_client_params()) as client:
                bekend_op_rij = 0
                for listing in client.iter_search(city, max_price=max_price,
                                                  sort="newest"):
                    if time.time() > deadline or len(nieuw) >= max_new:
                        break
                    url = getattr(listing, "url", "") or ""
                    if not url:
                        continue
                    if url in known:
                        bekend_op_rij += 1
                        # 12 bekenden op rij => we zitten voorbij het verse deel
                        if bekend_op_rij >= 12:
                            break
                        continue
                    bekend_op_rij = 0
                    # één detail-call, zodat de score compleet is
                    try:
                        full = client.listing(listing.id) if listing.id else listing
                    except Exception:
                        full = listing
                    d = _to_dict(full or listing)
                    if not d:
                        continue
                    if d.get("price_m2") and d["price_m2"] > max_price_m2:
                        known.add(url)
                        continue
                    nieuw.append(d)
                    known.add(url)
                    time.sleep(random.uniform(delay * 0.5, delay))
        except Exception as e:
            if _is_block(e):
                geblokkeerd.append(city)
            print(f"[quickscan] {city}: {str(e)[:90]}", flush=True)
            continue

    toegevoegd = 0
    if nieuw:
        toegevoegd, _ = upsert_listings(nieuw)
        compute_scores()

    rapport = {"nieuw": toegevoegd, "gezien": len(nieuw),
               "steden": len(cities), "geblokkeerd": geblokkeerd}
    log_run("funda_quickscan", "ok" if not geblokkeerd else "blocked",
            found=len(nieuw), new=toegevoegd,
            message="" if not geblokkeerd else f"geblokkeerd: {', '.join(geblokkeerd)}")

    # Direct melden bij een goede kans — dit is de hele reden van de snelle scan
    if toegevoegd:
        try:
            from ..notify import check_and_alert, notify_enabled
            if notify_enabled():
                rapport["alerts"] = check_and_alert()
        except Exception as e:
            rapport["alert_error"] = str(e)[:200]
    print(f"[quickscan] klaar — {toegevoegd} nieuw toegevoegd", flush=True)
    return rapport
