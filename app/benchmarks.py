"""Benchmark-engine: €/m²-percentielen per (stad|wijk) × grootte-segment.

Basis is het VERKOCHTE aanbod (sold_listings) van de laatste BENCHMARK_MONTHS
maanden — dat is wat de markt daadwerkelijk betaalt, niet wat verkopers hopen.
Fallback-trap bij het scoren van een object:

  1. wijk-verkocht   (zelfde wijk, zelfde segment, n ≥ BENCHMARK_MIN_WIJK)
  2. stad-verkocht   (zelfde stad, zelfde segment,  n ≥ BENCHMARK_MIN_STAD)
  3. stad-actief     (eigen actieve aanbod, zelfde stad+segment — noodgreep)

Segmenten (kleine objecten zijn per m² structureel duurder):
  klein  < 50 m² · midden 50–100 m² · groot > 100 m²
Grenzen instelbaar via SEGMENT_SMALL_MAX / SEGMENT_MID_MAX.
"""
from __future__ import annotations

import datetime as dt
import os
import statistics

from .db import Benchmark, Listing, SessionLocal, SoldListing

SEGMENTS = ("klein", "midden", "groot")


def _small_max() -> float:
    return float(os.getenv("SEGMENT_SMALL_MAX", "50"))


def _mid_max() -> float:
    return float(os.getenv("SEGMENT_MID_MAX", "100"))


def segment_of(living_area: float | None) -> str | None:
    if not living_area or living_area <= 0:
        return None
    if living_area < _small_max():
        return "klein"
    if living_area <= _mid_max():
        return "midden"
    return "groot"


def pc4(postcode: str | None) -> str:
    digits = "".join(ch for ch in (postcode or "") if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else ""


def wijk_key(neighbourhood: str | None, postcode: str | None) -> str:
    """Wijknaam van Funda; postcode-4 als vangnet wanneer die ontbreekt."""
    w = (neighbourhood or "").strip().lower()
    return w or pc4(postcode)


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 4:
        med = statistics.median(values)
        return min(values), med, max(values)
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], q[1], q[2]


def _sane(price_m2: float | None, living: float | None) -> bool:
    return bool(price_m2 and 400 <= price_m2 <= 30000 and living and living >= 15)


def compute_benchmarks() -> dict:
    """Herbereken alle benchmarks. Verkocht = leidend; actief = fallback."""
    months = int(os.getenv("BENCHMARK_MONTHS", "12"))
    horizon = dt.datetime.utcnow() - dt.timedelta(days=int((months + 4) * 30.5))

    stad: dict[tuple[str, str], list[float]] = {}
    wijk: dict[tuple[str, str, str], list[float]] = {}
    actief: dict[tuple[str, str], list[float]] = {}

    with SessionLocal() as s:
        for r in s.query(SoldListing).all():
            if not _sane(r.price_m2, r.living_area):
                continue
            pub = None
            try:
                pub = dt.datetime.fromisoformat(
                    (r.publication_date or "").replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
            if (pub or r.scraped or dt.datetime.utcnow()) < horizon:
                continue
            seg = segment_of(r.living_area)
            city = (r.city or "").strip().lower()
            if not seg or not city:
                continue
            stad.setdefault((city, seg), []).append(r.price_m2)
            wk = wijk_key(r.neighbourhood, r.postcode)
            if wk:
                wijk.setdefault((city, wk, seg), []).append(r.price_m2)

        for r in (s.query(Listing)
                  .filter(Listing.is_demo.is_(False),
                          Listing.price_m2.isnot(None), Listing.price_m2 > 0)):
            if not _sane(r.price_m2, r.living_area):
                continue
            seg = segment_of(r.living_area)
            city = (r.city or "").strip().lower()
            if seg and city:
                actief.setdefault((city, seg), []).append(r.price_m2)

        # herbouw benchmark-tabel
        s.query(Benchmark).delete()
        now = dt.datetime.utcnow()
        n_rows = 0
        for (city, seg), vals in stad.items():
            p25, med, p75 = _percentiles(vals)
            s.add(Benchmark(scope="stad", basis="verkocht", city=city, area="",
                            segment=seg, n=len(vals), p25=round(p25), median=round(med),
                            p75=round(p75), updated=now))
            n_rows += 1
        for (city, wk, seg), vals in wijk.items():
            p25, med, p75 = _percentiles(vals)
            s.add(Benchmark(scope="wijk", basis="verkocht", city=city, area=wk,
                            segment=seg, n=len(vals), p25=round(p25), median=round(med),
                            p75=round(p75), updated=now))
            n_rows += 1
        for (city, seg), vals in actief.items():
            p25, med, p75 = _percentiles(vals)
            s.add(Benchmark(scope="stad", basis="actief", city=city, area="",
                            segment=seg, n=len(vals), p25=round(p25), median=round(med),
                            p75=round(p75), updated=now))
            n_rows += 1
        s.commit()

    return {"benchmarks": n_rows, "steden_verkocht": len({c for c, _ in stad}),
            "wijken_verkocht": len({(c, w) for c, w, _ in wijk})}


class BenchmarkMap:
    """Alle benchmarks één keer geladen, met de getrapte lookup."""

    def __init__(self) -> None:
        self.min_wijk = int(os.getenv("BENCHMARK_MIN_WIJK", "15"))
        self.min_stad = int(os.getenv("BENCHMARK_MIN_STAD", "8"))
        self._wijk: dict = {}
        self._stad: dict = {}
        self._actief: dict = {}
        with SessionLocal() as s:
            for b in s.query(Benchmark).all():
                if b.scope == "wijk":
                    self._wijk[(b.city, b.area, b.segment)] = b
                elif b.basis == "verkocht":
                    self._stad[(b.city, b.segment)] = b
                else:
                    self._actief[(b.city, b.segment)] = b

    def lookup(self, city: str | None, neighbourhood: str | None,
               postcode: str | None, living_area: float | None):
        """→ (Benchmark, label) of (None, '')."""
        seg = segment_of(living_area)
        c = (city or "").strip().lower()
        if not seg or not c:
            return None, ""
        wk = wijk_key(neighbourhood, postcode)
        b = self._wijk.get((c, wk, seg)) if wk else None
        if b and b.n >= self.min_wijk:
            return b, f"wijk {wk} · {seg} (verkocht, n={b.n})"
        b = self._stad.get((c, seg))
        if b and b.n >= self.min_stad:
            return b, f"{c} · {seg} (verkocht, n={b.n})"
        b = self._actief.get((c, seg))
        if b and b.n >= 3:
            return b, f"{c} · {seg} (actief aanbod, n={b.n})"
        return None, ""


def scoreboard(city: str = "") -> dict:
    """Scorebord voor het dashboard: per stad × segment (+ wijken)."""
    with SessionLocal() as s:
        q = s.query(Benchmark)
        if city:
            q = q.filter(Benchmark.city == city.strip().lower())
        rows = [b.to_dict() for b in q.all()]
    out: dict = {}
    for b in rows:
        cs = out.setdefault(b["city"], {"stad": {}, "wijken": {}, "actief": {}})
        if b["scope"] == "stad" and b["basis"] == "verkocht":
            cs["stad"][b["segment"]] = b
        elif b["scope"] == "stad":
            cs["actief"][b["segment"]] = b
        else:
            cs["wijken"].setdefault(b["area"], {})[b["segment"]] = b
    return out
