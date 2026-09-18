"""Database — SQLAlchemy. Postgres op Railway (DATABASE_URL), lokaal SQLite."""
from __future__ import annotations

import datetime as dt
import json
import os

from sqlalchemy import (Boolean, DateTime, Float, Integer, String, Text,
                        create_engine, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./flipradar.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def days_on_market(published: str | None) -> int | None:
    """Dagen sinds publicatie op de bron. Hét onderhandelsignaal: een woning
    die lang staat heeft een verkoper die wil praten — vaak nog vóór de
    eerste prijsverlaging."""
    if not published:
        return None
    raw = str(published).strip()
    try:
        d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                d = dt.datetime.strptime(raw[:10], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    days = (dt.datetime.utcnow() - d).days
    return days if 0 <= days < 4000 else None


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True, default="funda")
    url: Mapped[str] = mapped_column(String(600), unique=True, index=True)
    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(100), index=True, default="")
    neighbourhood: Mapped[str] = mapped_column(String(120), default="")
    postcode: Mapped[str] = mapped_column(String(10), default="")
    province: Mapped[str] = mapped_column(String(60), default="")

    price: Mapped[float] = mapped_column(Float, nullable=True)
    living_area: Mapped[float] = mapped_column(Float, nullable=True)
    plot_area: Mapped[float] = mapped_column(Float, nullable=True)
    price_m2: Mapped[float] = mapped_column(Float, nullable=True)
    property_type: Mapped[str] = mapped_column(String(120), default="")
    build_year: Mapped[int] = mapped_column(Integer, nullable=True)
    rooms: Mapped[int] = mapped_column(Integer, nullable=True)
    floors: Mapped[int] = mapped_column(Integer, nullable=True)   # woonlagen -> splitsen per laag
    energy_label: Mapped[str] = mapped_column(String(8), default="")
    maintenance: Mapped[str] = mapped_column(String(30), default="")
    erfpacht: Mapped[bool] = mapped_column(Boolean, default=False)
    vve_monthly: Mapped[str] = mapped_column(String(30), default="")

    flag_casco: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_verbouw: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_ontwikkeling: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_dakopbouw: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_uitbreiden: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_splits_bouwkundig: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_splits_kadastraal: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_splitsvergunning: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_verhuurd: Mapped[bool] = mapped_column(Boolean, default=False)
    context: Mapped[str] = mapped_column(Text, default="")

    # Publicatiedatum op de bron (Funda "aangeboden sinds") -> days-on-market.
    # Let op: verschilt van first_seen (wanneer ONZE scraper het object zag).
    published: Mapped[str] = mapped_column(String(40), default="")

    auction_date: Mapped[str] = mapped_column(String(40), default="")
    photo_url: Mapped[str] = mapped_column(String(600), default="")
    broker: Mapped[str] = mapped_column(String(200), default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    price_history: Mapped[str] = mapped_column(Text, default="[]")

    flip_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_breakdown: Mapped[str] = mapped_column(Text, default="")

    # Benchmark waartegen dit object gescoord is (wijk/stad × segment)
    bench_label: Mapped[str] = mapped_column(String(160), default="")
    bench_median: Mapped[float] = mapped_column(Float, nullable=True)
    discount_pct: Mapped[float] = mapped_column(Float, nullable=True)

    # Oordeel van het agent-team (leeg zolang er geen ANTHROPIC_API_KEY is).
    # ai_splits: 'ja' | 'nee' | 'onzeker' — wat de tekst zegt over splitsen.
    ai_splits: Mapped[str] = mapped_column(String(10), default="")
    ai_units: Mapped[int] = mapped_column(Integer, nullable=True)
    ai_punten: Mapped[int] = mapped_column(Integer, nullable=True)   # -20..+20
    ai_samenvatting: Mapped[str] = mapped_column(Text, default="")
    ai_bevinding: Mapped[str] = mapped_column(Text, default="")      # volle JSON
    ai_advies: Mapped[str] = mapped_column(String(20), default="")   # criticus
    ai_checked: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "address": self.address,
            "city": self.city,
            "neighbourhood": self.neighbourhood,
            "postcode": self.postcode,
            "province": self.province,
            "price": self.price,
            "living_area": self.living_area,
            "plot_area": self.plot_area,
            "price_m2": self.price_m2,
            "property_type": self.property_type,
            "build_year": self.build_year,
            "rooms": self.rooms,
            "floors": self.floors,
            "energy_label": self.energy_label,
            "maintenance": self.maintenance,
            "erfpacht": self.erfpacht,
            "vve_monthly": self.vve_monthly,
            "flags": {
                "casco": self.flag_casco,
                "verbouw": self.flag_verbouw,
                "ontwikkeling": self.flag_ontwikkeling,
                "dakopbouw": self.flag_dakopbouw,
                "uitbreiden": self.flag_uitbreiden,
                "splits_bouwkundig": self.flag_splits_bouwkundig,
                "splits_kadastraal": self.flag_splits_kadastraal,
                "splitsvergunning": self.flag_splitsvergunning,
                "verhuurd": self.flag_verhuurd,
            },
            "context": self.context,
            "published": self.published,
            "days_on_market": days_on_market(self.published),
            "auction_date": self.auction_date,
            "photo_url": self.photo_url,
            "broker": self.broker,
            "is_demo": self.is_demo,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "price_history": json.loads(self.price_history or "[]"),
            "flip_score": self.flip_score,
            "score_breakdown": self.score_breakdown,
            "bench_label": self.bench_label,
            "bench_median": self.bench_median,
            "discount_pct": self.discount_pct,
            "ai_splits": self.ai_splits or "",
            "ai_units": self.ai_units,
            "ai_punten": self.ai_punten,
            "ai_samenvatting": self.ai_samenvatting or "",
            "ai_advies": self.ai_advies or "",
            "ai_bevinding": json.loads(self.ai_bevinding) if self.ai_bevinding else None,
            "ai_checked": self.ai_checked.isoformat() if self.ai_checked else None,
        }


class SoldListing(Base):
    """Verkocht object (alleen zoekresultaat-data — geen detail-calls nodig).
    Vormt de basis voor de wijk/stad × segment-benchmarks."""
    __tablename__ = "sold_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(600), unique=True, index=True)
    address: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(100), index=True, default="")
    neighbourhood: Mapped[str] = mapped_column(String(120), index=True, default="")
    postcode: Mapped[str] = mapped_column(String(10), default="")
    price: Mapped[float] = mapped_column(Float, nullable=True)
    living_area: Mapped[float] = mapped_column(Float, nullable=True)
    plot_area: Mapped[float] = mapped_column(Float, nullable=True)
    price_m2: Mapped[float] = mapped_column(Float, nullable=True)
    property_type: Mapped[str] = mapped_column(String(120), default="")
    publication_date: Mapped[str] = mapped_column(String(40), default="")
    scraped: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class Benchmark(Base):
    """Marktbenchmark: €/m²-percentielen per (stad|wijk) × segment.
    basis 'verkocht' = uit sold_listings; 'actief' = fallback uit eigen aanbod."""
    __tablename__ = "benchmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(10), index=True)      # 'stad' | 'wijk'
    basis: Mapped[str] = mapped_column(String(10), default="verkocht")
    city: Mapped[str] = mapped_column(String(100), index=True)
    area: Mapped[str] = mapped_column(String(120), default="")      # wijknaam of pc4 ('' bij stad)
    segment: Mapped[str] = mapped_column(String(10))                # 'klein' | 'midden' | 'groot'
    n: Mapped[int] = mapped_column(Integer, default=0)
    p25: Mapped[float] = mapped_column(Float, nullable=True)
    median: Mapped[float] = mapped_column(Float, nullable=True)
    p75: Mapped[float] = mapped_column(Float, nullable=True)
    updated: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    def to_dict(self) -> dict:
        return {"scope": self.scope, "basis": self.basis, "city": self.city,
                "area": self.area, "segment": self.segment, "n": self.n,
                "p25": self.p25, "median": self.median, "p75": self.p75,
                "updated": self.updated.isoformat() if self.updated else None}


class Profile(Base):
    """Aannamenprofiel voor de scenario-engine (wekelijks aanpasbaar)."""
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    params: Mapped[str] = mapped_column(Text, default="{}")
    updated: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class AlertSent(Base):
    """Verstuurde telefoonmeldingen — voorkomt dubbele alerts per object."""
    __tablename__ = "alerts_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, index=True)
    deal_score: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class GemeenteRegel(Base):
    """Splitsbeleid per gemeente, uitgezocht door de Regelchecker.

    Dit is de duurste informatie per object én de minst veranderlijke: beleid
    wijzigt een paar keer per jaar, dus één keer opzoeken per gemeente en
    hergebruiken. Amsterdam en Utrecht verbieden splitsen grotendeels,
    Rotterdam staat het vaak toe — dat verschil bepaalt of een 'splitsdeal'
    bestaat of niet.
    """
    __tablename__ = "gemeente_regels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    toegestaan: Mapped[str] = mapped_column(String(24), default="onbekend")
    # ja | ja_met_vergunning | beperkt | nee | onbekend
    vergunning_nodig: Mapped[bool] = mapped_column(Boolean, default=True)
    min_woning_m2: Mapped[float] = mapped_column(Float, nullable=True)
    zekerheid: Mapped[str] = mapped_column(String(10), default="laag")
    samenvatting: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[str] = mapped_column(Text, default="{}")
    bronnen: Mapped[str] = mapped_column(Text, default="[]")
    updated: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    def to_dict(self) -> dict:
        return {"city": self.city, "toegestaan": self.toegestaan,
                "vergunning_nodig": self.vergunning_nodig,
                "min_woning_m2": self.min_woning_m2, "zekerheid": self.zekerheid,
                "samenvatting": self.samenvatting,
                "details": json.loads(self.details or "{}"),
                "bronnen": json.loads(self.bronnen or "[]"),
                "updated": self.updated.isoformat() if self.updated else None}


class AgentKosten(Base):
    """Dagteller voor modelkosten — de rem op de agents."""
    __tablename__ = "agent_kosten"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dag: Mapped[str] = mapped_column(String(10), index=True)
    agent: Mapped[str] = mapped_column(String(30), default="")
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    tok_in: Mapped[int] = mapped_column(Integer, default=0)
    tok_out: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    laatste: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)


class AgentRonde(Base):
    """Eén ronde van het agent-team: wanneer, waarom, wat kostte het, en
    welke objecten kregen daardoor een andere score."""
    __tablename__ = "agent_rondes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gestart: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    klaar: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schema")   # schema | knop
    status: Mapped[str] = mapped_column(String(20), default="bezig")
    rapport: Mapped[str] = mapped_column(Text, default="{}")
    kosten_usd: Mapped[float] = mapped_column(Float, default=0.0)
    wijzigingen: Mapped[str] = mapped_column(Text, default="[]")         # score voor/na

    def to_dict(self, volledig: bool = False) -> dict:
        wz = json.loads(self.wijzigingen or "[]")
        d = {"id": self.id, "gestart": self.gestart.isoformat() if self.gestart else None,
             "klaar": self.klaar.isoformat() if self.klaar else None,
             "trigger": self.trigger, "status": self.status,
             "kosten_usd": round(self.kosten_usd or 0, 4),
             "aantal_wijzigingen": len(wz)}
        if volledig:
            d["rapport"] = json.loads(self.rapport or "{}")
            d["wijzigingen"] = wz
        return d


class AgentActie(Base):
    """Logboek: één regel per modelaanroep. Hierin staat letterlijk wat de
    agent te zien kreeg, wat hij antwoordde, welke bronnen hij gebruikte, wat
    de code met dat antwoord deed en wat het effect op de score was.
    Zo is elk oordeel in het dashboard terug te herleiden."""
    __tablename__ = "agent_acties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tijd: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    ronde_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(30), index=True)
    stap: Mapped[str] = mapped_column(String(30), default="")      # beoordeling | zoeken | omzetten
    model: Mapped[str] = mapped_column(String(60), default="")
    listing_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    stad: Mapped[str] = mapped_column(String(100), default="", index=True)
    onderwerp: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | fout
    fout: Mapped[str] = mapped_column(Text, default="")
    invoer: Mapped[str] = mapped_column(Text, default="")          # wat de agent zag
    antwoord: Mapped[str] = mapped_column(Text, default="")        # ruw antwoord
    bronnen: Mapped[str] = mapped_column(Text, default="[]")
    toegepast: Mapped[str] = mapped_column(Text, default="")       # wat de code ermee deed
    effect: Mapped[str] = mapped_column(Text, default="")          # score voor/na
    tok_in: Mapped[int] = mapped_column(Integer, default=0)
    tok_out: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    duur_ms: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self, volledig: bool = False) -> dict:
        def _j(x, leeg):
            try:
                return json.loads(x) if x else leeg
            except ValueError:
                return x
        d = {"id": self.id, "tijd": self.tijd.isoformat() if self.tijd else None,
             "ronde_id": self.ronde_id, "agent": self.agent, "stap": self.stap,
             "model": self.model, "listing_id": self.listing_id, "stad": self.stad,
             "onderwerp": self.onderwerp, "status": self.status,
             "fout": self.fout, "tok_in": self.tok_in, "tok_out": self.tok_out,
             "usd": round(self.usd or 0, 5), "duur_ms": self.duur_ms,
             "toegepast": _j(self.toegepast, None), "effect": _j(self.effect, None),
             "aantal_bronnen": len(_j(self.bronnen, []) or [])}
        # Korte samenvatting van het antwoord voor de lijstweergave
        a = _j(self.antwoord, None)
        if isinstance(a, dict):
            d["kern"] = (a.get("samenvatting") or a.get("splitsbaar")
                         or a.get("toegestaan") or "")
        else:
            d["kern"] = (self.antwoord or "")[:160]
        if volledig:
            d["invoer"] = self.invoer
            d["antwoord"] = a if a is not None else self.antwoord
            d["bronnen"] = _j(self.bronnen, [])
        return d


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    started: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Lichte migratie: nieuwe kolommen toevoegen aan een bestaande tabel
    # (create_all voegt geen kolommen toe aan tabellen die al bestaan).
    from sqlalchemy import inspect, text
    try:
        existing = {c["name"] for c in inspect(engine).get_columns("listings")}
        for _name, _ddl in {"neighbourhood": "VARCHAR(120) DEFAULT ''",
                            "photo_url": "VARCHAR(600) DEFAULT ''",
                            "bench_label": "VARCHAR(160) DEFAULT ''",
                            "bench_median": "FLOAT",
                            "discount_pct": "FLOAT",
                            "published": "VARCHAR(40) DEFAULT ''",
                            "floors": "INTEGER",
                            "ai_splits": "VARCHAR(10) DEFAULT ''",
                            "ai_units": "INTEGER",
                            "ai_punten": "INTEGER",
                            "ai_samenvatting": "TEXT",
                            "ai_bevinding": "TEXT",
                            "ai_advies": "VARCHAR(20) DEFAULT ''",
                            "ai_checked": "TIMESTAMP"}.items():
            if _name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE listings ADD COLUMN {_name} {_ddl}"))
    except Exception as e:
        print(f"[db] migratie overgeslagen: {e}")

    # Zelfde truc voor de agent-tabellen, zodat een bestaande installatie
    # niet omvalt als er later een kolom bijkomt.
    for _tabel, _kolommen in {
            "agent_kosten": {"laatste": "TIMESTAMP"},
            "gemeente_regels": {"zekerheid": "VARCHAR(10) DEFAULT 'laag'",
                                "min_woning_m2": "FLOAT"}}.items():
        try:
            have = {c["name"] for c in inspect(engine).get_columns(_tabel)}
            for _name, _ddl in _kolommen.items():
                if _name not in have:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {_tabel} ADD COLUMN {_name} {_ddl}"))
        except Exception as e:
            print(f"[db] migratie {_tabel} overgeslagen: {e}")


_UPDATABLE = {
    c.name for c in Listing.__table__.columns
} - {"id", "first_seen", "last_seen", "price_history", "flip_score", "score_breakdown",
     "bench_label", "bench_median", "discount_pct",
     # Het oordeel van de agents hoort niet bij een scrape-update: dat zou het
     # bij elke herhaalde scrape wissen.
     "ai_splits", "ai_units", "ai_punten", "ai_samenvatting", "ai_bevinding",
     "ai_advies", "ai_checked"}


def boek_agent_kosten(agent: str, usd: float, tok_in: int, tok_out: int) -> None:
    """Telt modelkosten per dag per agent op (de rem in agents/__init__.py)."""
    dag = dt.date.today().isoformat()
    with SessionLocal() as s:
        row = s.query(AgentKosten).filter_by(dag=dag, agent=agent).one_or_none()
        if row is None:
            row = AgentKosten(dag=dag, agent=agent)
            s.add(row)
        row.usd = (row.usd or 0) + usd
        row.tok_in = (row.tok_in or 0) + tok_in
        row.tok_out = (row.tok_out or 0) + tok_out
        row.calls = (row.calls or 0) + 1
        row.laatste = dt.datetime.utcnow()
        s.commit()


def agent_kosten_vandaag() -> float:
    from sqlalchemy import func as _f
    dag = dt.date.today().isoformat()
    with SessionLocal() as s:
        return float(s.query(_f.coalesce(_f.sum(AgentKosten.usd), 0.0))
                     .filter(AgentKosten.dag == dag).scalar() or 0.0)


def agent_kosten_overzicht(dagen: int = 7) -> list[dict]:
    from sqlalchemy import func as _f
    grens = (dt.date.today() - dt.timedelta(days=dagen)).isoformat()
    with SessionLocal() as s:
        rows = (s.query(AgentKosten.dag, AgentKosten.agent,
                        _f.sum(AgentKosten.usd), _f.sum(AgentKosten.calls))
                .filter(AgentKosten.dag >= grens)
                .group_by(AgentKosten.dag, AgentKosten.agent)
                .order_by(AgentKosten.dag.desc()).all())
        return [{"dag": d, "agent": a, "usd": round(float(u or 0), 4),
                 "calls": int(c or 0)} for d, a, u, c in rows]


# ── Logboek van het agent-team ──────────────────────────────────
# Invoer wordt ingekort opgeslagen; het antwoord en het effect altijd volledig.
LOG_INVOER_MAX = int(os.getenv("AGENT_LOG_INVOER_MAX", "12000"))


def ronde_start(trigger: str = "schema") -> int:
    with SessionLocal() as s:
        r = AgentRonde(trigger=trigger[:20], status="bezig")
        s.add(r)
        s.commit()
        return r.id


def ronde_klaar(ronde_id: int, status: str, rapport: dict,
                kosten_usd: float, wijzigingen: list[dict]) -> None:
    with SessionLocal() as s:
        r = s.get(AgentRonde, ronde_id)
        if not r:
            return
        r.klaar = dt.datetime.utcnow()
        r.status = status[:20]
        r.rapport = json.dumps(rapport, ensure_ascii=False, default=str)
        r.kosten_usd = kosten_usd
        r.wijzigingen = json.dumps(wijzigingen, ensure_ascii=False, default=str)
        s.commit()


def actie_log(**kw) -> int:
    """Schrijft één logregel. Faalt nooit hard: een logfout mag de agent
    niet stilleggen, maar wordt wel geprint."""
    try:
        invoer = kw.pop("invoer", "") or ""
        if len(invoer) > LOG_INVOER_MAX:
            invoer = invoer[:LOG_INVOER_MAX] + f"\n[… ingekort, {len(invoer)} tekens totaal]"
        for k in ("antwoord", "bronnen", "toegepast", "effect"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False, default=str)
        velden = {c.name for c in AgentActie.__table__.columns} - {"id"}
        with SessionLocal() as s:
            a = AgentActie(invoer=invoer, **{k: v for k, v in kw.items() if k in velden})
            s.add(a)
            s.commit()
            return a.id
    except Exception as e:
        print(f"[agents] logregel niet opgeslagen: {e}", flush=True)
        return 0


def actie_update(actie_id: int, **kw) -> None:
    if not actie_id:
        return
    try:
        with SessionLocal() as s:
            a = s.get(AgentActie, actie_id)
            if not a:
                return
            for k, v in kw.items():
                if k in ("toegepast", "effect", "antwoord", "bronnen") and not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False, default=str)
                setattr(a, k, v)
            s.commit()
    except Exception as e:
        print(f"[agents] logregel niet bijgewerkt: {e}", flush=True)


def acties_effect_zetten(ronde_id: int, listing_id: int, effect: dict) -> None:
    """Koppelt de scorewijziging van een object aan alle logregels die in
    deze ronde over dat object gingen."""
    with SessionLocal() as s:
        for a in (s.query(AgentActie)
                  .filter_by(ronde_id=ronde_id, listing_id=listing_id).all()):
            a.effect = json.dumps(effect, ensure_ascii=False, default=str)
        s.commit()


def acties_lijst(agent: str = "", listing_id: int | None = None, stad: str = "",
                 ronde_id: int | None = None, status: str = "",
                 limit: int = 100, voor_id: int | None = None) -> list[dict]:
    with SessionLocal() as s:
        q = s.query(AgentActie)
        if agent:
            q = q.filter(AgentActie.agent == agent)
        if listing_id:
            q = q.filter(AgentActie.listing_id == listing_id)
        if stad:
            q = q.filter(func.lower(AgentActie.stad) == stad.lower())
        if ronde_id:
            q = q.filter(AgentActie.ronde_id == ronde_id)
        if status:
            q = q.filter(AgentActie.status == status)
        if voor_id:
            q = q.filter(AgentActie.id < voor_id)      # bladeren naar ouder
        return [a.to_dict() for a in
                q.order_by(AgentActie.id.desc()).limit(limit).all()]


def actie_detail(actie_id: int) -> dict | None:
    with SessionLocal() as s:
        a = s.get(AgentActie, actie_id)
        return a.to_dict(volledig=True) if a else None


def rondes_lijst(limit: int = 30) -> list[dict]:
    with SessionLocal() as s:
        return [r.to_dict() for r in
                s.query(AgentRonde).order_by(AgentRonde.id.desc()).limit(limit).all()]


def ronde_detail(ronde_id: int) -> dict | None:
    with SessionLocal() as s:
        r = s.get(AgentRonde, ronde_id)
        if not r:
            return None
        d = r.to_dict(volledig=True)
        from sqlalchemy import case
        per_agent = {}
        for agent, n, fouten in (
                s.query(AgentActie.agent, func.count(),
                        func.sum(case((AgentActie.status == "fout", 1), else_=0)))
                .filter(AgentActie.ronde_id == ronde_id)
                .group_by(AgentActie.agent).all()):
            per_agent[agent] = {"aanroepen": n, "fouten": int(fouten or 0)}
        d["per_agent"] = per_agent
        return d


def agent_werk_overzicht() -> dict:
    """Wat het agent-team tot nu toe heeft gedaan — de harde cijfers achter
    het dashboardpaneel. Geen modelaanroepen, puur tellen in de database."""
    from sqlalchemy import func as _f

    with SessionLocal() as s:
        gelezen = s.query(Listing).filter(Listing.ai_checked.isnot(None)).count()
        totaal = s.query(Listing).filter(Listing.is_demo.is_(False)).count()
        splits = {k or "leeg": n for k, n in
                  s.query(Listing.ai_splits, _f.count())
                  .filter(Listing.ai_checked.isnot(None))
                  .group_by(Listing.ai_splits)}
        advies = {k or "geen": n for k, n in
                  s.query(Listing.ai_advies, _f.count())
                  .filter(Listing.ai_advies != "")
                  .group_by(Listing.ai_advies)}
        laatst_gelezen = s.query(_f.max(Listing.ai_checked)).scalar()
        gem = s.query(GemeenteRegel).all()
        per_agent = {}
        for agent, usd, calls, laatste in (
                s.query(AgentKosten.agent, _f.sum(AgentKosten.usd),
                        _f.sum(AgentKosten.calls), _f.max(AgentKosten.laatste))
                .group_by(AgentKosten.agent).all()):
            per_agent[agent] = {"usd_totaal": round(float(usd or 0), 4),
                                "calls_totaal": int(calls or 0),
                                "laatste": laatste.isoformat() if laatste else None}
        run = (s.query(ScrapeRun).filter_by(source="agents")
               .order_by(ScrapeRun.started.desc()).first())
        laatste_ronde = ({"gestart": run.started.isoformat(), "status": run.status,
                          "gelezen": run.found, "bekritiseerd": run.new,
                          "bericht": run.message} if run else None)

    verouderd = 0
    for g in gem:
        if not g.updated or (dt.datetime.utcnow() - g.updated).days >= 30:
            verouderd += 1
    return {
        "objecten_totaal": totaal,
        "gelezen": gelezen,
        "splits_verdeling": splits,
        "advies_verdeling": advies,
        "laatst_gelezen": laatst_gelezen.isoformat() if laatst_gelezen else None,
        "gemeenten": len(gem),
        "gemeenten_verouderd": verouderd,
        "gemeenten_beleid": {g.city: g.toegestaan for g in gem},
        "per_agent": per_agent,
        "laatste_ronde": laatste_ronde,
    }


def gemeente_regel(city: str) -> dict | None:
    if not city:
        return None
    with SessionLocal() as s:
        row = (s.query(GemeenteRegel)
               .filter(func.lower(GemeenteRegel.city) == city.lower())
               .one_or_none())
        return row.to_dict() if row else None


def gemeente_regels_alle() -> list[dict]:
    with SessionLocal() as s:
        return [r.to_dict() for r in
                s.query(GemeenteRegel).order_by(GemeenteRegel.city).all()]


def gemeente_regel_opslaan(city: str, data: dict) -> dict:
    """Schrijft/ververst het splitsbeleid van één gemeente."""
    with SessionLocal() as s:
        row = (s.query(GemeenteRegel)
               .filter(func.lower(GemeenteRegel.city) == city.lower())
               .one_or_none())
        if row is None:
            row = GemeenteRegel(city=city.lower())
            s.add(row)
        row.toegestaan = (data.get("toegestaan") or "onbekend")[:24]
        row.vergunning_nodig = bool(data.get("vergunning_nodig", True))
        mm = data.get("min_woning_m2")
        row.min_woning_m2 = float(mm) if isinstance(mm, (int, float)) else None
        row.zekerheid = (data.get("zekerheid") or "laag")[:10]
        row.samenvatting = data.get("samenvatting") or ""
        row.details = json.dumps(data.get("details") or {}, ensure_ascii=False)
        row.bronnen = json.dumps(data.get("bronnen") or [], ensure_ascii=False)
        row.updated = dt.datetime.utcnow()
        s.commit()
        return row.to_dict()


def upsert_listings(items: list[dict]) -> tuple[int, int]:
    """Insert nieuwe listings, update bestaande. Houdt prijshistorie bij."""
    now = dt.datetime.utcnow()
    new = updated = 0
    with SessionLocal() as s:
        for it in items:
            data = {k: v for k, v in it.items() if k in _UPDATABLE and v is not None}
            url = data.get("url")
            if not url:
                continue
            obj = s.query(Listing).filter_by(url=url).one_or_none()
            if obj:
                new_price = data.get("price")
                if new_price and obj.price and abs(new_price - obj.price) > 1:
                    hist = json.loads(obj.price_history or "[]")
                    hist.append({"date": now.date().isoformat(),
                                 "from": obj.price, "to": new_price})
                    obj.price_history = json.dumps(hist)
                for k, v in data.items():
                    setattr(obj, k, v)
                obj.last_seen = now
                updated += 1
            else:
                s.add(Listing(**data, first_seen=now, last_seen=now))
                new += 1
        s.commit()
    return new, updated


_SOLD_UPDATABLE = {c.name for c in SoldListing.__table__.columns} - {"id", "scraped"}


def upsert_sold(items: list[dict]) -> tuple[int, int]:
    """Insert/refresh verkochte objecten (op url)."""
    now = dt.datetime.utcnow()
    new = updated = 0
    with SessionLocal() as s:
        for it in items:
            data = {k: v for k, v in it.items() if k in _SOLD_UPDATABLE and v is not None}
            url = data.get("url")
            if not url:
                continue
            obj = s.query(SoldListing).filter_by(url=url).one_or_none()
            if obj:
                for k, v in data.items():
                    setattr(obj, k, v)
                updated += 1
            else:
                s.add(SoldListing(**data, scraped=now))
                new += 1
        s.commit()
    return new, updated


def log_run(source: str, status: str, found: int = 0, new: int = 0, message: str = "") -> None:
    with SessionLocal() as s:
        s.add(ScrapeRun(source=source, status=status, found=found,
                        new=new, message=(message or "")[:500]))
        s.commit()
