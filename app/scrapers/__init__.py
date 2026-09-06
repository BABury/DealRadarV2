"""Scraper-registry. Funda schrijft per stad weg (incrementeel)."""
from __future__ import annotations

import threading
import traceback

from ..db import log_run, upsert_listings, upsert_sold
from ..scoring import compute_scores
from .funda_source import scrape_funda, scrape_funda_sold
from .veilingen import scrape_biedboek, scrape_bog_auctions, scrape_veilingnotaris

SCRAPERS = {
    "veilingnotaris": scrape_veilingnotaris,
    "bog_auctions": scrape_bog_auctions,
    "biedboek": scrape_biedboek,
}

PROGRESS: dict = {}
_lock = threading.Lock()


def run_all(sources=None) -> dict:
    report: dict = {}
    with _lock:
        PROGRESS.clear()

    if not sources or "funda" in sources:
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
            if city_items:
                new, _ = upsert_listings(city_items)
                with _lock:
                    fu["found"] += len(city_items)
                    fu["new"] += new
                compute_scores()

        def set_total(n):
            with _lock:
                fu["cities_total"] = n

        try:
            all_items = scrape_funda(sink=sink, on_total=set_total)
            status = "ok" if fu["ok_cities"] > 0 else "error"
            msg = "" if status == "ok" else "Alle steden geblokkeerd (403) - zet SCRAPER_PROXY."
            log_run("funda", status, found=len(all_items), new=fu["new"], message=msg)
            report["funda"] = {"status": status, "found": len(all_items),
                               "new": fu["new"], "ok_cities": fu["ok_cities"],
                               "cities": fu["cities_total"]}
        except Exception as e:
            log_run("funda", "error", message=f"{e}\n{traceback.format_exc()[:400]}")
            report["funda"] = {"status": "error", "message": str(e)[:200]}

    for name, fn in SCRAPERS.items():
        if sources and name not in sources:
            continue
        try:
            items = fn()
            new, updated = upsert_listings(items)
            log_run(name, "ok", found=len(items), new=new)
            report[name] = {"status": "ok", "found": len(items), "new": new, "updated": updated}
        except Exception as e:
            log_run(name, "error", message=f"{e}\n{traceback.format_exc()[:400]}")
            report[name] = {"status": "error", "message": str(e)[:200]}

    scored = compute_scores()
    report["_scored"] = scored
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
