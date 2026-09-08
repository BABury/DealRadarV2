#!/usr/bin/env python3
"""Lokale taak (elke 4 dagen): scrape op je eigen (thuis-)IP en schrijf de
Top-10 development-P&L als Excel naar je bureaublad.

Waarom lokaal: Funda blokkeert datacenter-IP's (Railway), maar niet je
thuisverbinding. Draai je dit met DATABASE_URL naar je Railway-Postgres, dan
vul je meteen dezelfde database die het online dashboard leest.

Gebruik:
    python run_local.py                # scrape + export naar ~/Desktop
    python run_local.py --no-scrape    # alleen export uit bestaande database

Omgevingsvariabelen:
    DATABASE_URL   Railway-Postgres (leeg = lokale SQLite flipradar.db)
    PNL_OUT_DIR    doelmap (default: ~/Desktop)
    PNL_REGION     grote_steden | randstad | regio_eindhoven | alle
    PNL_N          aantal objecten (default 10)
    FUNDA_CITIES   komma-lijst steden om te scrapen
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    do_scrape = "--no-scrape" not in sys.argv
    out_dir = os.getenv("PNL_OUT_DIR", str(Path.home() / "Desktop"))

    from app.db import init_db
    init_db()

    if do_scrape:
        from app.scrapers import run_all, run_sold
        print("[run_local] verkocht-scrape (benchmarks)…", flush=True)
        try:
            run_sold()
        except Exception as e:
            print("  verkocht-scrape fout:", e, flush=True)
        print("[run_local] aanbod-scrape…", flush=True)
        try:
            run_all()
        except Exception as e:
            print("  aanbod-scrape fout:", e, flush=True)

    from app.export_pnl import build_pnl_workbook
    path = build_pnl_workbook(
        out_dir,
        region=os.getenv("PNL_REGION", "grote_steden"),
        n=int(os.getenv("PNL_N", "10")),
    )
    print(f"[run_local] ✅ workbook opgeslagen: {path}", flush=True)


if __name__ == "__main__":
    main()
