"""Database — SQLAlchemy. Postgres op Railway (DATABASE_URL), lokaal SQLite."""
from __future__ import annotations

import datetime as dt
import json
import os

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./flipradar.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


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
                            "discount_pct": "FLOAT"}.items():
            if _name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE listings ADD COLUMN {_name} {_ddl}"))
    except Exception as e:
        print(f"[db] migratie overgeslagen: {e}")


_UPDATABLE = {
    c.name for c in Listing.__table__.columns
} - {"id", "first_seen", "last_seen", "price_history", "flip_score", "score_breakdown",
     "bench_label", "bench_median", "discount_pct"}


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
