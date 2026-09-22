"""FlipRadar — FastAPI app: dashboard + API + dagelijkse scrape-scheduler."""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import desc, func

from .db import Listing, ScrapeRun, SessionLocal, init_db
from .scoring import compute_scores

STATIC = Path(__file__).parent / "static"
_scheduler: BackgroundScheduler | None = None
_refresh_lock = threading.Lock()
_run_state = {"running": False, "started": None, "last_report": None}


def _run_scrape(sources: list[str] | None = None,
                funda_steden: list[str] | None = None,
                funda_budget: int | None = None) -> dict:
    import datetime as dt
    from .scrapers import run_all
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "al bezig"}
    _run_state["running"] = True
    _run_state["started"] = dt.datetime.utcnow().isoformat()
    try:
        report = run_all(sources, funda_steden=funda_steden, funda_budget=funda_budget)
        _run_state["last_report"] = report
        return report
    finally:
        _run_state["running"] = False
        _refresh_lock.release()


def _run_daily() -> dict:
    """Geplande ochtendrun + statusbericht op Telegram, zodat je elke dag weet
    dat DealRadar nog draait (en ziet wat het opleverde)."""
    report = _run_scrape()
    try:
        from .notify import notify_enabled, send_daily_status
        if notify_enabled() and isinstance(report, dict) and report.get("status") != "al bezig":
            send_daily_status(report)
    except Exception as e:
        print(f"[status] bericht mislukt: {e}", flush=True)
    return report


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


_agent_lock = threading.Lock()
_agent_state = {"running": False, "started": None, "last_report": None}


def _run_agents(max_lezen: int | None = None, top_n: int | None = None,
                trigger: str = "schema") -> dict:
    """Agent-team: leest advertenties, checkt gemeentebeleid, bekritiseert de
    topdeals. Eigen lock (mag prima naast een scrape lopen — het is netwerk-
    wachten, geen browserwerk) maar nooit twee rondes tegelijk."""
    import datetime as dt
    if trigger == "schema":
        # Het team werkt alleen op opdracht, tenzij je automatische rondes aanzet
        from .agents.instellingen import lees as _lees
        if not _lees()["automatische_rondes"]:
            return {"status": "overgeslagen", "reden": "automatische rondes staan uit"}
    from .agents import hoofdschakelaar_aan, stop_wissen
    if not hoofdschakelaar_aan():
        return {"status": "uit", "reden": "de hoofdschakelaar van het team staat uit"}
    if not _agent_lock.acquire(blocking=False):
        return {"status": "al bezig"}
    stop_wissen()
    _agent_state["running"] = True
    _agent_state["started"] = dt.datetime.utcnow().isoformat()
    try:
        from .agents.team import run_team
        report = run_team(max_lezen=max_lezen, top_n=top_n, trigger=trigger)
        _agent_state["last_report"] = report
        return report
    except Exception as e:
        print(f"[agents] ronde mislukt: {e}", flush=True)
        return {"status": "error", "message": str(e)[:200]}
    finally:
        _agent_state["running"] = False
        _agent_lock.release()


# Opdrachten per object: status per listing_id, zodat dashboard en Telegram
# kunnen zien wanneer het team klaar is.
ONDERZOEK: dict[int, dict] = {}


def _run_onderzoek(listing_id: int, trigger: str = "knop", na=None) -> dict:
    """Laat het team één object uitzoeken. `na(rapport)` wordt aangeroepen als
    het klaar is (bijvoorbeeld: antwoord terugsturen in Telegram)."""
    import datetime as dt
    from .agents import hoofdschakelaar_aan, stop_wissen
    if not hoofdschakelaar_aan():
        rapport = {"status": "uit", "reden": "de hoofdschakelaar van het team staat uit",
                   "listing_id": listing_id}
        ONDERZOEK[listing_id] = {"status": "klaar", "rapport": rapport}
        if na:
            na(rapport)
        return rapport
    if not _agent_lock.acquire(blocking=False):
        rapport = {"status": "al bezig", "listing_id": listing_id}
        if na:
            na(rapport)
        return rapport
    stop_wissen()
    _agent_state["running"] = True
    _agent_state["started"] = dt.datetime.utcnow().isoformat()
    ONDERZOEK[listing_id] = {"status": "bezig", "gestart": _agent_state["started"]}
    try:
        from .agents.team import onderzoek_object
        rapport = onderzoek_object(listing_id, trigger=trigger)
    except Exception as e:
        print(f"[agents] onderzoek {listing_id} mislukt: {e}", flush=True)
        rapport = {"status": "error", "message": str(e)[:200], "listing_id": listing_id}
    finally:
        _agent_state["running"] = False
        _agent_lock.release()
    ONDERZOEK[listing_id] = {"status": "klaar", "rapport": rapport,
                             "klaar": dt.datetime.utcnow().isoformat()}
    _agent_state["last_report"] = rapport
    if na:
        try:
            na(rapport)
        except Exception as e:
            print(f"[agents] terugmelden mislukt: {e}", flush=True)
    return rapport


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
    _scheduler.add_job(_run_daily, CronTrigger(hour=hour, minute=0),
                       id="daily_scrape", max_instances=1)
    # Wekelijkse verkocht-scrape → benchmarks (verkocht-data verandert langzaam)
    # Snelle scan op nieuw aanbod — de echte edge: goede deals zijn binnen
    # 24-48u weg, dus we kijken elke QUICKSCAN_MINUTES minuten of er iets
    # nieuws online staat en melden dat direct.
    # Extra Funda-runs gespreid over de dag (elke run 4 steden, roterend).
    # Funda blokkeert op tempo, dus liever vaker kleine porties dan één grote.
    # Met 6 focussteden en 2 steden per run komt zo elke stad 2× per dag langs.
    for i, h in enumerate(os.getenv("FUNDA_HOURS", "9,12,15,18,21").split(",")):
        if h.strip().isdigit():
            _scheduler.add_job(_run_scrape, CronTrigger(hour=int(h), minute=0),
                               args=[["funda"]], id=f"funda_extra_{i}", max_instances=1)

    # Agent-team: kort ná de Funda-runs, zodat het nieuwe aanbod meteen gelezen
    # en bekritiseerd wordt. Zonder ANTHROPIC_API_KEY doen deze jobs niets.
    for i, h in enumerate(os.getenv("AGENT_HOURS", "7,18").split(",")):
        if h.strip().isdigit():
            _scheduler.add_job(_run_agents, CronTrigger(hour=int(h), minute=30),
                               id=f"agents_{i}", max_instances=1)

    qs_min = int(os.getenv("QUICKSCAN_MINUTES", "0"))   # pyfunda-API is geblokkeerd -> standaard uit
    if qs_min > 0:
        _scheduler.add_job(_run_quickscan, CronTrigger(minute=f"*/{qs_min}"),
                           id="quickscan", max_instances=1)

    sold_day = os.getenv("SOLD_SCRAPE_DAY", "sun")
    sold_hour = int(os.getenv("SOLD_SCRAPE_HOUR", "3"))
    _scheduler.add_job(_run_sold_scrape, CronTrigger(day_of_week=sold_day,
                                                     hour=sold_hour, minute=0),
                       id="weekly_sold_scrape", max_instances=1)
    _scheduler.start()
    try:
        from .db import rondes_afbreken
        n = rondes_afbreken()
        if n:
            print(f"[agents] {n} ronde(s) door herstart afgebroken", flush=True)
    except Exception as e:
        print(f"[agents] afgebroken rondes niet bijgewerkt: {e}", flush=True)
    from .scenarios import seed_profiles
    seed_profiles()
    # Telegram-opdrachten: vertel Telegram waar de berichten heen moeten
    if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_WEBHOOK", "1") == "1":
        from .telegram_bot import koppel_webhook
        threading.Thread(target=koppel_webhook, daemon=True).start()
    if os.getenv("SCRAPE_ON_START", "1") == "1":       # na elke deploy direct resultaten
        threading.Thread(target=_run_scrape, daemon=True).start()
    # Eerste keer: nog geen verkocht-data? Dan direct benchmarks opbouwen.
    if os.getenv("SOLD_SCRAPE_ON_START", "0") == "1":   # pyfunda-verkocht is geblokkeerd -> uit
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
    sort: str = Query(default="score"),
    focus: int = Query(default=1),
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with SessionLocal() as s:
        qry = s.query(Listing)
        if city:
            qry = qry.filter(func.lower(Listing.city) == city.lower())
        elif focus:
            # Standaard alleen de focussteden; focus=0 toont heel Nederland
            from .agents.instellingen import focus_varianten
            qry = qry.filter(func.lower(func.trim(Listing.city)).in_(sorted(focus_varianten())))
        if source:
            qry = qry.filter(Listing.source == source)
        if min_score:
            qry = qry.filter(Listing.flip_score >= min_score)
        if q:
            qry = qry.filter(Listing.address.ilike(f"%{q}%"))
        # score (standaard) | nieuw = laatst gevonden eerst | oud = langst in de lijst
        volgorde = {"nieuw": desc(Listing.first_seen),
                    "oud": Listing.first_seen.asc()}.get(sort, desc(Listing.flip_score))
        # offset: de lijst in het dashboard laadt in porties door
        rows = qry.order_by(volgorde).offset(offset).limit(limit).all()
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
         region: str = Query(default="focus"),
         n: int = Query(default=5, le=25),
         min_score: int = Query(default=0),
         rank: str = Query(default="risk"),
         soort: str = Query(default="koop"),
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
    region = region if region in ("focus", "grote_steden", "randstad", "regio_eindhoven", "alle") else "focus"
    soort = soort if soort in ("alles", "koop", "veiling", "project") else "koop"
    return top_listings(params, cities=city_list, n=n, min_score=min_score,
                        rank=rank, region=region, soort=soort)


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
def refresh(source: str = Query(default=""),
            cities: str = Query(default=""),
            budget: int = Query(default=0, le=400)):
    """Scrape starten. `cities` = nu precies deze steden (geen rotatie), bv.
    cities=amsterdam,eindhoven; `budget` = pagina's voor deze run (Funda
    blokkeert op tempo, dus houd het onder ±200 per half uur)."""
    sources = [source] if source else None
    steden = [c.strip() for c in cities.split(",") if c.strip()] or None
    if steden and not sources:
        sources = ["funda"]
    threading.Thread(target=_run_scrape, args=(sources, steden, budget or None),
                     daemon=True).start()
    return {"status": "gestart", "sources": sources or "alle",
            "steden": steden or "volgens rotatie", "budget": budget or "standaard"}


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


@app.get("/api/diag/funda")
def diag_funda(city: str = Query(default="eindhoven")):
    """Test of een echte browser vanaf deze server langs Funda's botcheck komt.
    Eén zoekpagina, niets wordt opgeslagen. Duurt ±20 seconden."""
    from .scrapers.funda_browser import diagnose
    return diagnose(city)


@app.get("/api/diag/funda-detail")
def diag_funda_detail(url: str = Query(default="")):
    """Vindt de scraper de omschrijving op een Funda-detailpagina? Eén pagina,
    niets wordt opgeslagen. Laat zien via welke route de tekst gevonden werd."""
    from .scrapers.funda_browser import diagnose_detail
    return diagnose_detail(url or None)


@app.get("/api/export-pnl")
def export_pnl(region: str = Query(default="focus"),
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
            "regio": os.getenv("ALERT_REGION", "focus"),
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


@app.get("/api/agents/status")
def agents_status():
    """Staat het agent-team aan, wat heeft het gekost, en wat weet het al?"""
    from .agents import status as agent_status
    from .db import agent_kosten_overzicht, gemeente_regels_alle
    st = agent_status()
    try:
        regels = gemeente_regels_alle()
    except Exception:
        regels = []
    return {**st, **_agent_state,
            "gemeenten_bekend": len(regels),
            "kosten_per_dag": agent_kosten_overzicht(7),
            "uren": os.getenv("AGENT_HOURS", "7,18")}


@app.get("/api/agents/overzicht")
def agents_overzicht():
    """Eén overzicht: wat elke agent doet, deed, vond en kostte.
    Dit is het paneel in het dashboard — geen modelaanroepen, alleen tellen."""
    import datetime as dt

    from .agents import PROGRESS, agents_enabled, dagbudget
    from .agents.instellingen import lees as lees_instellingen
    from .agents.instellingen import schatting
    from .agents.team import wachtrij
    ins = lees_instellingen()
    from .db import agent_kosten_overzicht, agent_kosten_vandaag, agent_werk_overzicht

    werk = agent_werk_overzicht()
    per_agent = werk["per_agent"]
    vandaag = agent_kosten_vandaag()
    kosten = agent_kosten_overzicht(7)
    vandaag_str = dt.date.today().isoformat()
    per_dag_agent: dict = {}
    for k in kosten:
        per_dag_agent.setdefault(k["agent"], {})[k["dag"]] = k["usd"]

    def rij(naam: str, taak: str, extra: dict) -> dict:
        p = PROGRESS.get(naam) or {}
        a = per_agent.get(naam, {})
        return {"naam": naam, "taak": taak,
                "bezig": bool(p.get("bezig")),
                "voortgang": {"gedaan": p.get("gedaan", 0), "totaal": p.get("totaal", 0),
                              "nu": p.get("nu", ""), "fouten": p.get("fouten", 0)},
                "laatste_ronde": p.get("klaar"),
                "laatste_ronde_resultaat": p.get("samenvatting", ""),
                "laatst_actief": a.get("laatste"),
                "aanroepen_totaal": a.get("calls_totaal", 0),
                "usd_totaal": a.get("usd_totaal", 0),
                "usd_vandaag": (per_dag_agent.get(naam, {}) or {}).get(vandaag_str, 0),
                **extra}

    return {
        "aan": agents_enabled(),
        "ronde_bezig": _agent_state["running"],
        "ronde_gestart": _agent_state["started"],
        "volgende_rondes": os.getenv("AGENT_HOURS", "7,18"),
        "budget": {"dag_usd": dagbudget(), "vandaag_usd": round(vandaag, 4),
                   "over_usd": round(max(0.0, dagbudget() - vandaag), 4)},
        "agents": [
            rij("lezer", "leest de advertentietekst van elk nieuw object",
                {"gelezen_totaal": werk["gelezen"],
                 "objecten_totaal": werk["objecten_totaal"],
                 "wachtrij": wachtrij(), "per_ronde": ins["lotte_per_ronde"],
                 "vanaf_m2": ins["lotte_min_m2"], "tot_m2": ins["lotte_max_m2"],
                 "aan_uit": ins["lotte_aan"], "verdeling": werk["splits_verdeling"],
                 "laatst_gelezen": werk["laatst_gelezen"]}),
            rij("regelchecker", "zoekt per gemeente het splitsbeleid op",
                {"gemeenten": werk["gemeenten"],
                 "gemeenten_verouderd": werk["gemeenten_verouderd"],
                 "per_ronde": ins["rik_steden_per_ronde"], "aan_uit": ins["rik_aan"],
                 "beleid": {c: t for c, t in werk["gemeenten_beleid"].items()
                            if c in set(ins["steden"])}}),
            rij("criticus", "valt de topdeals aan en levert de vragen vooraf",
                {"per_ronde": ins["kees_top_n"], "aan_uit": ins["kees_aan"],
                 "verdeling": werk["advies_verdeling"]}),
        ],
        "laatste_ronde": werk["laatste_ronde"],
        "focussteden": ins["steden"],
        "automatisch": ins["automatische_rondes"],
        "team_aan": ins["team_aan"],
        "schatting_per_ronde": schatting(ins),
        "laatste_rapport": _agent_state["last_report"],
        "kosten_per_dag": kosten,
    }


@app.get("/api/agents/logboek")
def agents_logboek(agent: str = Query(default=""),
                   listing_id: int = Query(default=0),
                   stad: str = Query(default=""),
                   ronde: int = Query(default=0),
                   status: str = Query(default=""),
                   voor_id: int = Query(default=0),
                   limit: int = Query(default=100, le=500)):
    """Logboek: elke modelaanroep, nieuwste eerst. Filters zijn te combineren.
    voor_id = bladeren naar oudere regels."""
    from .db import acties_lijst
    return acties_lijst(agent=agent, listing_id=listing_id or None, stad=stad,
                        ronde_id=ronde or None, status=status, limit=limit,
                        voor_id=voor_id or None)


@app.get("/api/agents/logboek/{actie_id}")
def agents_logboek_detail(actie_id: int):
    """Eén logregel volledig: wat de agent zag, wat hij antwoordde, welke
    bronnen, wat de code ermee deed en het effect op de score."""
    from .db import actie_detail
    d = actie_detail(actie_id)
    if not d:
        return JSONResponse({"error": f"logregel {actie_id} niet gevonden"}, status_code=404)
    return d


@app.get("/api/agents/rondes")
def agents_rondes(limit: int = Query(default=30, le=200)):
    from .db import rondes_lijst
    return rondes_lijst(limit)


@app.get("/api/agents/rondes/{ronde_id}")
def agents_ronde_detail(ronde_id: int):
    """Eén ronde: rapport, kosten, per agent het aantal aanroepen en fouten,
    en welke objecten een andere score kregen (voor → na)."""
    from .db import ronde_detail
    d = ronde_detail(ronde_id)
    if not d:
        return JSONResponse({"error": f"ronde {ronde_id} niet gevonden"}, status_code=404)
    return d


@app.get("/api/agents/instructies")
def agents_instructies():
    """De letterlijke opdracht die elke agent krijgt, plus de regels waarmee
    de code zijn antwoord begrenst. Geen geheime prompts."""
    from .agents import MODEL_DENKER, MODEL_LEZER, dagbudget
    from .agents import criticus, lezer, regels
    return {
        "lezer": {"model": MODEL_LEZER, "instructie": lezer.SYSTEM,
                  "antwoordformaat": lezer.SCHEMA,
                  "grenzen": ["alleen objecten in de focussteden, binnen de m²-grenzen, met tekst "
                              "en (standaard) met splitspotentie volgens de rekensom","punten altijd tussen −20 en +20",
                              "huurbeding ingeroepen = altijd −20",
                              "verhuurd = hooguit −8",
                              "al gesplitst = hooguit −5",
                              "'nee' zet de splitsanalyse op niet-splitsbaar",
                              "'ja' met letterlijk citaat telt als 'genoemd in advertentie'"]},
        "regelchecker": {"model": MODEL_DENKER, "instructie": regels.SYSTEM_ZOEK,
                         "omzetten": regels.SYSTEM_JSON,
                         "antwoordformaat": regels.SCHEMA,
                         "grenzen": [f"beleid wordt {regels.geldig_dagen()} dagen bewaard",
                                     "alleen focussteden, en alleen als het beleid nog niet (geldig) bekend is",
                                     "zekerheidsfactor splitsen: ja 1,00 · met vergunning 0,90 · "
                                     "beperkt 0,65 · nee 0,25",
                                     "niets gevonden = niet opgeslagen en geen effect (1,00); volgende ronde opnieuw",
                                     "minimale woninggrootte van de gemeente gaat vóór de aanname",
                                     "een bestaande splitsingsvergunning wordt nooit afgewaardeerd"]},
        "criticus": {"model": MODEL_DENKER, "instructie": criticus.SYSTEM,
                     "antwoordformaat": criticus.SCHEMA,
                     "grenzen": ["alleen de topdeals (koop en veiling) in de focussteden; "
                                 "dezelfde deal pas na 14 dagen opnieuw","aftrek hooguit −30",
                                 "alleen lage ernst: hooguit −5",
                                 "geen hoge ernst: hooguit −15",
                                 "Lezer + Criticus samen tussen −35 en +20",
                                 "'laten lopen' blokkeert de Telegram-melding, niet het dashboard"]},
        "budget": {"dag_usd": dagbudget()},
    }


@app.get("/api/agents/instellingen")
def agents_instellingen_lezen():
    """Huidige instellingen van het team, de standaarden en de kostenschatting."""
    from .agents.instellingen import GRENZEN, STANDAARD, lees, schatting
    from .scenarios import GROTE_STEDEN
    d = lees()
    keuze = sorted({c for c in GROTE_STEDEN if not c.startswith("'")} | set(d["steden"]))
    return {"instellingen": d, "standaard": STANDAARD, "grenzen": GRENZEN,
            "steden_keuze": keuze, "schatting_per_ronde": schatting(d)}


@app.post("/api/agents/instellingen")
def agents_instellingen_opslaan(body: dict):
    """Instellingen bijwerken. Getallen worden begrensd (zie 'grenzen')."""
    from .agents.instellingen import opslaan, schatting
    d = opslaan(body or {})
    try:
        compute_scores()          # focus/grenzen kunnen de ranglijst veranderen
    except Exception as e:
        print(f"[agents] herscoren na instellingen mislukt: {e}", flush=True)
    return {"instellingen": d, "schatting_per_ronde": schatting(d)}


@app.post("/api/agents/stop")
def agents_stop():
    """Stopknop: het team maakt de aanroep af die al onderweg is en stopt dan."""
    from .agents import stop_aanvragen
    stop_aanvragen()
    return {"status": "stop gevraagd", "was_bezig": _agent_state["running"],
            "uitleg": "Een aanroep die al onderweg is wordt nog afgemaakt; daarna stopt het team."}


@app.post("/api/agents/schakelaar")
def agents_schakelaar(aan: int = Query(...)):
    """Hoofdschakelaar. Uit = niets kan het team starten, en wat loopt stopt."""
    from .agents import stop_aanvragen
    from .agents.instellingen import opslaan
    d = opslaan({"team_aan": bool(aan)})
    if not aan:
        stop_aanvragen()
    return {"team_aan": d["team_aan"], "was_bezig": _agent_state["running"]}


@app.post("/api/agents/onderzoek")
def agents_onderzoek(listing_id: int = Query(...)):
    """Laat het team één object uitzoeken (Rik → Lotte → Kees)."""
    from .agents import agents_enabled, hoofdschakelaar_aan
    if not agents_enabled():
        return JSONResponse({"error": "Geen ANTHROPIC_API_KEY ingesteld."}, status_code=400)
    if not hoofdschakelaar_aan():
        return JSONResponse({"error": "De hoofdschakelaar van het team staat uit."}, status_code=400)
    if _agent_state["running"]:
        return JSONResponse({"error": "Het team is al bezig — probeer het zo nog eens."},
                            status_code=409)
    with SessionLocal() as s:
        if not s.get(Listing, listing_id):
            return JSONResponse({"error": f"object {listing_id} niet gevonden"}, status_code=404)
    ONDERZOEK[listing_id] = {"status": "bezig"}
    threading.Thread(target=_run_onderzoek, args=(listing_id, "knop"), daemon=True).start()
    return {"status": "gestart", "listing_id": listing_id}


@app.get("/api/agents/onderzoek/{listing_id}")
def agents_onderzoek_status(listing_id: int):
    return ONDERZOEK.get(listing_id) or {"status": "geen"}


def _ronde_via_telegram() -> None:
    """/ronde vanuit Telegram: draai een ronde en meld het resultaat terug."""
    from .telegram_bot import publieke_url, stuur
    r = _run_agents(trigger="telegram")
    if r.get("status") == "al bezig":
        stuur("⏳ Het team is al bezig. Probeer het zo nog eens.")
        return
    if r.get("status") in ("uit", "budget_op", "error"):
        stuur(f"⚠️ Ronde niet gelukt: {r.get('reden') or r.get('message') or r.get('status')}")
        return
    lz, rk, ks = r.get("lezer") or {}, r.get("regelchecker") or {}, r.get("criticus") or {}
    url = publieke_url()
    stuur(f"✅ <b>Ronde {r.get('ronde_id')} klaar</b>\n"
          f"📖 Lotte las {lz.get('gelezen', 0)} objecten\n"
          f"⚖️ Rik zocht {rk.get('gecheckt', 0)} gemeenten uit\n"
          f"🔎 Kees bekritiseerde {ks.get('beoordeeld', 0)} deals\n"
          f"<i>kosten ${(r.get('kosten') or {}).get('deze_ronde_usd', 0):.3f}</i>"
          + (f" · <a href=\"{url}/?ronde={r.get('ronde_id')}\">wat veranderde er</a>" if url else ""))


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Berichten van Telegram. Zonder het juiste geheim: genegeerd."""
    from .telegram_bot import geheim, verwerk
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != geheim():
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    threading.Thread(target=verwerk, args=(update, _run_onderzoek, _ronde_via_telegram),
                     daemon=True).start()
    return {"ok": True}        # meteen antwoorden; Telegram probeert anders opnieuw


@app.post("/api/telegram/koppel")
def telegram_koppel():
    """Webhook opnieuw aanmelden bij Telegram (gebeurt ook bij elke start)."""
    from .telegram_bot import koppel_webhook
    return koppel_webhook()


@app.post("/api/agents/run")
def agents_run(max_lezen: int = Query(default=0, le=500),
               top_n: int = Query(default=0, le=25)):
    """Laat het team nu een ronde doen (leest, checkt beleid, bekritiseert)."""
    from .agents import agents_enabled
    if not agents_enabled():
        return JSONResponse(
            {"error": "Geen ANTHROPIC_API_KEY ingesteld. Zet die als variabele "
                      "in Railway; zonder key werkt de app zoals voorheen."},
            status_code=400)
    from .agents import hoofdschakelaar_aan
    if not hoofdschakelaar_aan():
        return JSONResponse({"error": "De hoofdschakelaar van het team staat uit."}, status_code=400)
    threading.Thread(target=_run_agents,
                     args=(max_lezen or None, top_n or None, "knop"), daemon=True).start()
    return {"status": "gestart"}


@app.get("/api/gemeente-regels")
def gemeente_regels_ep(city: str = Query(default="")):
    """Splitsbeleid per gemeente, zoals de Regelchecker het vond."""
    from .db import gemeente_regel, gemeente_regels_alle
    if city:
        return gemeente_regel(city) or {}
    return gemeente_regels_alle()


@app.post("/api/gemeente-regels")
def gemeente_regels_check(city: str = Query(min_length=2),
                          forceer: bool = Query(default=False)):
    """Zoek het splitsbeleid van één gemeente nu op (kost een paar cent)."""
    from .agents import AgentUit
    from .agents.regels import check
    try:
        return check(city, forceer=forceer)
    except AgentUit as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Regelcheck mislukt: {e}"}, status_code=502)


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
