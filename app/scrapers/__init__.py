"""Scraper-registry. Funda schrijft per stad weg (incrementeel)."""
from __future__ import annotations

import os
import threading
import traceback

from ..db import log_run, upsert_listings, upsert_sold
from ..property_filter import filter_items
from ..scoring import compute_scores
from .funda_browser import scrape_funda_browser
from .funda_source import scrape_funda, scrape_funda_sold
from .quickscan import quick_scan  # noqa: F401  (gebruikt door de scheduler)
from .vastgoedveiling import scrape_vastgoedveiling
from .veilingen import scrape_biedboek, scrape_bog_auctions, scrape_veilingnotaris

SCRAPERS = {
    "vastgoedveiling": scrape_vastgoedveiling,
    "veilingnotaris": scrape_veilingnotaris,
    "bog_auctions": scrape_bog_auctions,
    "biedboek": scrape_biedboek,
}

PROGRESS: dict = {}
_lock = threading.Lock()


def run_all(sources=None, funda_steden: list[str] | None = None,
            funda_budget: int | None = None) -> dict:
    report: dict = {}
    with _lock:
        PROGRESS.clear()

    # Op Railway uitzetten (FUNDA_ENABLED=0): Funda blokkeert datacenter-IP's en
    # vereist een echte browser — dat doet de lokale scraper op je Mac.
    funda_aan = os.getenv("FUNDA_ENABLED", "1") != "0"
    if funda_aan and (not sources or "funda" in sources):
        fu = {"cities_total": 0, "cities_done": 0, "found": 0, "new": 0,
              "ok_cities": 0, "current": "", "per_city": {}}
        with _lock:
            PROGRESS["funda"] = fu

        def sink(city, city_items, status):
            with _lock:
                fu["cities_done"] += 1
                fu["current"] = city
                fu["per_city"][city] = {"status": status, "found": len(city_items)}
                if status == "ok":
                    fu["ok_cities"] += 1
            city_items = filter_items(city_items, f"funda/{city}")
            if city_items:
                new, _ = upsert_listings(city_items)
                with _lock:
                    fu["found"] += len(city_items)
                    fu["new"] += new
                compute_scores()

        def set_total(n):
            with _lock:
                fu["cities_total"] = n

        # Funda's mobiele API (pyfunda) wordt sinds sept 2026 geblokkeerd;
        # de browser-variant komt er wél langs. Terug naar de API kan met
        # FUNDA_METHOD=api.
        scraper = (scrape_funda if os.getenv("FUNDA_METHOD", "browser") == "api"
                   else scrape_funda_browser)
        try:
            if funda_steden and scraper is scrape_funda_browser:
                all_items = scraper(sink=sink, on_total=set_total, steden=funda_steden,
                                    budget_override=funda_budget)
            else:
                all_items = scraper(sink=sink, on_total=set_total)
            status = "ok" if fu["ok_cities"] > 0 else "error"
            msg = "" if status == "ok" else "Alle steden geblokkeerd (403) - zet SCRAPER_PROXY."
            log_run("funda", status, found=len(all_items), new=fu["new"], message=msg)
            report["funda"] = {"status": status, "found": len(all_items),
                               "new": fu["new"], "ok_cities": fu["ok_cities"],
                               "cities": fu["cities_total"]}
        except Exception as e:
            log_run("funda", "error", message=f"{e}\n{traceback.format_exc()[:400]}")
            report["funda"] = {"status": "error", "message": str(e)[:200]}

    # Bronnen uitzetten die voor jouw doel niets opleveren, bv.
    # DISABLE_SOURCES="bog_auctions,veilingnotaris" (bedrijfspanden).
    uit = {s.strip().lower() for s in
           os.getenv("DISABLE_SOURCES", "").split(",") if s.strip()}

    for name, fn in SCRAPERS.items():
        if sources and name not in sources:
            continue
        if name in uit and not (sources and name in sources):
            print(f"[{name}] overgeslagen (DISABLE_SOURCES)", flush=True)
            continue
        try:
            items = fn()
            gevonden = len(items)
            items = filter_items(items, name)
            new, updated = upsert_listings(items)
            log_run(name, "ok", found=gevonden, new=new)
            report[name] = {"status": "ok", "found": gevonden, "woningen": len(items),
                            "new": new, "updated": updated}
        except Exception as e:
            log_run(name, "error", message=f"{e}\n{traceback.format_exc()[:400]}")
            report[name] = {"status": "error", "message": str(e)[:200]}

    scored = compute_scores()
    report["_scored"] = scored

    # Telefoonmelding bij nieuwe topdeals (alleen als notificaties zijn ingesteld)
    try:
        from ..notify import check_and_alert, notify_enabled
        if notify_enabled():
            report["_alerts"] = check_and_alert()
    except Exception as e:
        report["_alert_error"] = str(e)[:200]
    return report


def run_sold() -> dict:
    """Verkocht-scrape: voedt de benchmarks. Wekelijks is genoeg — verkocht-
    data verandert langzaam. Na afloop: benchmarks + scores herberekenen."""
    from ..benchmarks import compute_benchmarks

    fu = {"cities_total": 0, "cities_done": 0, "found": 0, "new": 0,
          "ok_cities": 0, "current": "", "per_city": {}}
    with _lock:
        PROGRESS["funda_sold"] = fu

    def sink(city, city_items, status):
        with _lock:
            fu["cities_done"] += 1
            fu["current"] = city
            fu["per_city"][city] = {"status": status, "found": len(city_items)}
            if status == "ok":
                fu["ok_cities"] += 1
        if city_items:
            new, _ = upsert_sold(city_items)
            with _lock:
                fu["found"] += len(city_items)
                fu["new"] += new

    def set_total(n):
        with _lock:
            fu["cities_total"] = n

    report: dict = {}
    try:
        items = scrape_funda_sold(sink=sink, on_total=set_total)
        status = "ok" if fu["ok_cities"] > 0 else "error"
        msg = "" if status == "ok" else "Alle steden geblokkeerd (403) - zet SCRAPER_PROXY."
        log_run("funda_sold", status, found=len(items), new=fu["new"], message=msg)
        report["funda_sold"] = {"status": status, "found": len(items),
                                "new": fu["new"], "ok_cities": fu["ok_cities"],
                                "cities": fu["cities_total"]}
    except Exception as e:
        log_run("funda_sold", "error", message=f"{e}\n{traceback.format_exc()[:400]}")
        report["funda_sold"] = {"status": "error", "message": str(e)[:200]}

    try:
        report["_benchmarks"] = compute_benchmarks()
        report["_scored"] = compute_scores()
    except Exception as e:
        report["_benchmark_error"] = str(e)[:200]
    return report
