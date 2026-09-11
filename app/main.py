"""FlipRadar — FastAPI app: dashboard + API + dagelijkse scrape-scheduler."""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import desc, func

from .db import Listing, ScrapeRun, SessionLocal, init_db
from .scoring import compute_scores

STATIC = Path(__file__).parent / "static"
_scheduler: BackgroundScheduler | None = None
_refresh_lock = threading.Lock()
_run_state = {"running": False, "started": None, "last_report": None}


def _run_scrape(sources: list[str] | None = None) -> dict:
    import datetime as dt
    from .scrapers import run_all
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "al bezig"}
    _run_state["running"] = True
    _run_state["started"] = dt.datetime.utcnow().isoformat()
    try:
        report = run_all(sources)
        _run_state["last_report"] = report
        return report
    finally:
        _run_state["running"] = False
        _refresh_lock.release()


def _run_quickscan() -> dict:
    """Snelle scan op nieuw aanbod. Gebruikt dezelfde lock als de grote
    scrapes: nooit twee scrapes tegelijk (en de nachtrun heeft voorrang)."""
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "overgeslagen (andere scrape bezig)"}
    try:
        from .scrapers import quick_scan
        return quick_scan()
    except Exception as e:
        print(f"[quickscan] fout: {e}", flush=True)
        return {"status": "error", "message": str(e)[:200]}
    finally:
        _refresh_lock.release()


def _run_sold_scrape() -> dict:
    """Verkocht-scrape (benchmarks). Zelfde lock: nooit twee scrapes tegelijk."""
    import datetime as dt
    from .scrapers import run_sold
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "al bezig"}
    _run_state["running"] = True
    _run_state["started"] = dt.datetime.utcnow().isoformat()
    try:
        report = run_sold()
        _run_state["last_report"] = report
        return report
    finally:
        _run_state["running"] = False
        _refresh_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    init_db()
    if os.getenv("SEED_ON_START", "1") == "1":
        from .seed import seed
        n = seed()
        if n:
            print(f"[seed] {n} demo-objecten geladen (lege database)")
    _scheduler = BackgroundScheduler(timezone="Europe/Amsterdam")
    hour = int(os.getenv("SCRAPE_HOUR", "6"))
    _scheduler.add_job(_run_scrape, CronTrigger(hour=hour, minute=0),
                       id="daily_scrape", max_instances=1)
    # Wekelijkse verkocht-scrape → benchmarks (verkocht-data verandert langzaam)
    # Snelle scan op nieuw aanbod — de echte edge: goede deals zijn binnen
    # 24-48u weg, dus we kijken elke QUICKSCAN_MINUTES minuten of er iets
    # nieuws online staat en melden dat direct.
    qs_min = int(os.getenv("QUICKSCAN_MINUTES", "20"))
    if qs_min > 0:
        _scheduler.add_job(_run_quickscan, CronTrigger(minute=f"*/{qs_min}"),
                           id="quickscan", max_instances=1)

    sold_day = os.getenv("SOLD_SCRAPE_DAY", "sun")
    sold_hour = int(os.getenv("SOLD_SCRAPE_HOUR", "3"))
    _scheduler.add_job(_run_sold_scrape, CronTrigger(day_of_week=sold_day,
                                                     hour=sold_hour, minute=0),
                       id="weekly_sold_scrape", max_instances=1)
    _scheduler.start()
    from .scenarios import seed_profiles
    seed_profiles()
    if os.getenv("SCRAPE_ON_START", "0") == "1":
        threading.Thread(target=_run_scrape, daemon=True).start()
    # Eerste keer: nog geen verkocht-data? Dan direct benchmarks opbouwen.
    if os.getenv("SOLD_SCRAPE_ON_START", "1") == "1":
        def _sold_if_empty():
            from .db import SoldListing
            with SessionLocal() as s:
                empty = s.query(SoldListing).first() is None
            if empty:
                print("[sold] geen verkocht-data — eerste benchmark-scrape start nu",
                      flush=True)
                _run_sold_scrape()
        threading.Thread(target=_sold_if_empty, daemon=True).start()
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="FlipRadar", lifespan=lifespan)


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/opportunities")
def opportunities(
    city: str = Query(default=""),
    source: str = Query(default=""),
    min_score: int = Query(default=0),
    q: str = Query(default=""),
    limit: int = Query(default=200, le=1000),
):
    with SessionLocal() as s:
        qry = s.query(Listing)
        if city:
            qry = qry.filter(func.lower(Listing.city) == city.lower())
        if source:
            qry = qry.filter(Listing.source == source)
        if min_score:
            qry = qry.filter(Listing.flip_score >= min_score)
        if q:
            qry = qry.filter(Listing.address.ilike(f"%{q}%"))
        rows = qry.order_by(desc(Listing.flip_score)).limit(limit).all()
        return [r.to_dict() for r in rows]


@app.get("/api/stats")
def stats():
    with SessionLocal() as s:
        total = s.query(Listing).count()
        avg = s.query(func.avg(Listing.flip_score)).scalar() or 0
        hot = s.query(Listing).filter(Listing.flip_score >= 60).count()
        cities = [c[0] for c in s.query(Listing.city).distinct() if c[0]]
        sources = {src: n for src, n in
                   s.query(Listing.source, func.count()).group_by(Listing.source)}
        demo = s.query(Listing).filter(Listing.is_demo.is_(True)).count()
        return {"total": total, "avg_score": round(float(avg), 1), "hot": hot,
                "cities": sorted(cities), "sources": sources, "demo": demo}


@app.get("/api/analyse")
def analyse_address(q: str = Query(min_length=3)):
    """Adres of Funda-link -> potentie-rapport (BAG + Kadaster + CBS + eigen comps)."""
    from .analyse import analyse
    try:
        return analyse(q)
    except Exception as e:
        return JSONResponse({"error": f"Analyse mislukt: {e}"}, status_code=502)


@app.get("/api/valuation")
def valuation(
    q: str = Query(default=""),
    listing_id: int = Query(default=0),
    area: float = Query(default=None),
    plot: float = Query(default=None),
    year: int = Query(default=None),
    label: str = Query(default=None),
    finish: int = Query(default=None, ge=1, le=5),
    maintenance: int = Query(default=None, ge=1, le=5),
    location: int = Query(default=None, ge=1, le=5),
    opex_pct: float = Query(default=None),
    rent_m2: float = Query(default=None),
    exclude: str = Query(default=""),
):
    """AI-taxatie: variabelen worden ingeschat, elke parameter is te overrulen.
    exclude = komma-gescheiden referentie-ids om buiten de waardering te laten."""
    from .valuation import valuate
    try:
        return valuate(q=q, listing_id=listing_id or None, exclude=exclude,
                       overrides={"area": area, "plot": plot, "year": year,
                                  "label": label, "finish": finish,
                                  "maintenance": maintenance, "location": location,
                                  "opex_pct": opex_pct, "rent_m2": rent_m2})
    except Exception as e:
        return JSONResponse({"error": f"Taxatie mislukt: {e}"}, status_code=502)


@app.get("/api/profiles")
def profiles():
    from .scenarios import list_profiles
    return list_profiles()


@app.post("/api/profiles")
def save_profile_ep(body: dict):
    from .scenarios import save_profile
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "naam ontbreekt"}, status_code=400)
    return save_profile(name, body.get("params") or {})


@app.get("/api/scenarios")
def scenarios_ep(listing_id: int = Query(...), profile: str = Query(default="standaard")):
    """Flip/splitsen/verhuur-tabel voor één object, tegen een aannamenprofiel."""
    from .scenarios import city_medians_all, get_profile, scenario_table
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if not row:
            return JSONResponse({"error": f"listing {listing_id} niet gevonden"}, status_code=404)
        d = row.to_dict()
    medians = city_medians_all()
    from .benchmarks import BenchmarkMap
    return scenario_table(d, get_profile(profile), medians.get((d.get("city") or "").lower()),
                          BenchmarkMap())


@app.get("/api/top5")
def top5(profile: str = Query(default="standaard"),
         cities: str = Query(default=""),
         region: str = Query(default="grote_steden"),
         n: int = Query(default=5, le=25),
         min_score: int = Query(default=0),
         rank: str = Query(default="risk"),
         min_area: float = Query(default=None),
         max_area: float = Query(default=None),
         min_price: float = Query(default=None),
         max_price: float = Query(default=None),
         max_price_m2: float = Query(default=None)):
    """Top-N objecten (regio Eindhoven standaard) op beste scenarioresultaat.
    rank=risk (risico-gecorrigeerd, default) | roi | winst.
    Filters komen uit het profiel; query-parameters overrulen ze eenmalig."""
    from .scenarios import get_profile, top_listings
    params = get_profile(profile)
    for k, v in (("min_area", min_area), ("max_area", max_area),
                 ("min_price", min_price), ("max_price", max_price),
                 ("max_price_m2", max_price_m2)):
        if v is not None:
            params[k] = v
    city_list = [c.strip() for c in cities.split(",") if c.strip()] or None
    rank = rank if rank in ("risk", "roi", "winst", "nieuw", "oud") else "risk"
    region = region if region in ("grote_steden", "randstad", "regio_eindhoven", "alle") else "grote_steden"
    return top_listings(params, cities=city_list, n=n, min_score=min_score,
                        rank=rank, region=region)


@app.get("/api/city-stats")
def city_stats():
    """Min/max/mediaan €/m² per stad, voor de marktcontext-kolommen."""
    with SessionLocal() as s:
        rows = (s.query(Listing.city, func.min(Listing.price_m2),
                        func.max(Listing.price_m2), func.count())
                .filter(Listing.price_m2.isnot(None), Listing.price_m2 > 0)
                .group_by(Listing.city).all())
        return {c.lower(): {"min": round(mn), "max": round(mx), "n": n}
                for c, mn, mx, n in rows if c}


@app.get("/api/running")
def running():
    try:
        from .scrapers import PROGRESS
        return {**_run_state, "progress": PROGRESS}
    except Exception:
        return _run_state


@app.get("/api/sources")
def source_status():
    with SessionLocal() as s:
        out = []
        for src in ("funda", "funda_quickscan", "funda_sold", "vastgoedveiling",
                    "veilingnotaris", "bog_auctions", "biedboek"):
            run = (s.query(ScrapeRun).filter_by(source=src)
                   .order_by(desc(ScrapeRun.started)).first())
            out.append({
                "source": src,
                "last_run": run.started.isoformat() if run else None,
                "status": run.status if run else "nog niet gedraaid",
                "found": run.found if run else 0,
                "message": run.message if run else "",
            })
        return out


@app.post("/api/refresh")
def refresh(source: str = Query(default="")):
    sources = [source] if source else None
    threading.Thread(target=_run_scrape, args=(sources,), daemon=True).start()
    return {"status": "gestart", "sources": sources or "alle"}


@app.post("/api/quickscan")
def quickscan_now():
    """Nu direct op nieuw aanbod scannen (en melden bij een goede kans)."""
    threading.Thread(target=_run_quickscan, daemon=True).start()
    return {"status": "gestart", "interval_minuten": int(os.getenv("QUICKSCAN_MINUTES", "20"))}


@app.post("/api/refresh-sold")
def refresh_sold():
    """Verkocht-scrape nu starten (voedt de benchmarks)."""
    threading.Thread(target=_run_sold_scrape, daemon=True).start()
    return {"status": "gestart", "bron": "funda_sold"}


@app.get("/api/benchmarks")
def benchmarks_ep(city: str = Query(default="")):
    """Marktscorebord: €/m² p25/mediaan/p75 per stad × segment, incl. wijken."""
    from .benchmarks import scoreboard
    return scoreboard(city)


@app.get("/api/export-pnl")
def export_pnl(region: str = Query(default="grote_steden"),
               profile: str = Query(default="standaard"),
               rank: str = Query(default="risk"),
               n: int = Query(default=10, le=25)):
    """Genereer de Top-N development-P&L als Excel en bied 'm als download aan."""
    import tempfile
    from .export_pnl import build_pnl_workbook
    try:
        path = build_pnl_workbook(tempfile.mkdtemp(), region=region,
                                  profile=profile, rank=rank, n=n)
    except Exception as e:
        return JSONResponse({"error": f"Export mislukt: {e}"}, status_code=502)
    return FileResponse(path, filename=Path(path).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/alerts/status")
def alerts_status():
    """Zijn telefoonmeldingen ingesteld, en met welke drempels?"""
    from .notify import notify_enabled
    return {
        "ingesteld": notify_enabled(),
        "kanalen": {
            "telegram": bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
            "pushover": bool(os.getenv("PUSHOVER_TOKEN") and os.getenv("PUSHOVER_USER")),
            "email": bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER")),
        },
        "drempels": {
            "min_winst": float(os.getenv("ALERT_MIN_PROFIT", "50000")),
            "min_roi_pct": float(os.getenv("ALERT_MIN_ROI", "15")),
            "regio": os.getenv("ALERT_REGION", "grote_steden"),
        },
    }


@app.post("/api/alerts/test")
def alerts_test():
    """Stuur een testmelding naar je telefoon."""
    from .notify import notify_enabled, send_email, send_pushover, send_telegram
    if not notify_enabled():
        return JSONResponse(
            {"error": "Geen meldingskanaal ingesteld. Zet TELEGRAM_TOKEN + "
                      "TELEGRAM_CHAT_ID (of PUSHOVER_*/SMTP_*) als variabelen."},
            status_code=400)
    txt = "✅ DealRadar testmelding — je meldingen werken."
    ok = False
    ok |= send_telegram(f"<b>DealRadar</b>\n{txt}")
    ok |= send_pushover("DealRadar", txt)
    ok |= send_email("DealRadar testmelding", txt)
    return {"verstuurd": ok}


@app.post("/api/alerts/run")
def alerts_run(region: str = Query(default=""), limit: int = Query(default=25, le=50)):
    """Controleer nu op nieuwe topdeals en meld ze."""
    from .notify import check_and_alert
    return check_and_alert(region=region, limit=limit)


@app.post("/api/rescore")
def rescore():
    return {"scored": compute_scores()}


@app.post("/api/purge-demo")
def purge_demo():
    with SessionLocal() as s:
        n = s.query(Listing).filter(Listing.is_demo.is_(True)).delete()
        s.commit()
    compute_scores()
    return {"verwijderd": n}
