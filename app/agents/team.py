"""Coördinator — verdeelt het werk over de agents en houdt de kosten laag.

De trechter is het hele idee:

  alle nieuwe objecten      -> Lezer      (goedkoop model, centen per object)
  steden van de kanshebbers -> Regelchecker (1x per gemeente per maand)
  alleen de topdeals        -> Criticus   (duur model, korte lijst)

Zo betaal je het dure denkwerk alleen voor objecten waar het over geld gaat.
Loopt het budget leeg, dan stopt elke agent netjes en pakt de volgende run de
achterstand op.
"""
from __future__ import annotations

import os
import traceback

from . import VERBRUIK, agents_enabled, budget_over, status
from . import criticus as agent_criticus
from . import lezer as agent_lezer
from . import regels as agent_regels

# Hoeveel werk per run. Ruim genoeg om bij te blijven, klein genoeg om niet
# in één keer het dagbudget op te maken.
MAX_LEZEN = int(os.getenv("AGENT_MAX_LEZEN", "120"))
MAX_STEDEN = int(os.getenv("AGENT_MAX_STEDEN", "4"))
TOP_N = int(os.getenv("AGENT_TOP_N", "5"))
MIN_M2 = float(os.getenv("AGENT_MIN_M2", "100"))
PROFIEL = os.getenv("AGENT_PROFIEL", os.getenv("ALERT_PROFILE", "bob"))


def _te_lezen(limit: int) -> list[int]:
    """Nog niet gelezen objecten, belangrijkste eerst.

    Voorwaarde: er moet iets te lezen zijn (tekst) óf het object moet groot
    genoeg zijn om te splitsen. Een klein appartement zonder omschrijving
    kost geld en levert niets op.
    """
    from sqlalchemy import desc, or_

    from ..db import Listing, SessionLocal
    with SessionLocal() as s:
        q = (s.query(Listing.id)
             .filter(Listing.is_demo.is_(False),
                     Listing.ai_checked.is_(None),
                     or_(Listing.living_area >= MIN_M2,
                         Listing.living_area.is_(None)),
                     or_(Listing.context != "", Listing.context.isnot(None)))
             .order_by(desc(Listing.flip_score), desc(Listing.first_seen))
             .limit(limit))
        return [r[0] for r in q.all()]


def _steden_van_kanshebbers(limit: int) -> list[str]:
    """Gemeenten waar we daadwerkelijk splitskandidaten hebben, met de meeste
    kandidaten eerst. Beleid opzoeken voor een stad zonder aanbod is weggegooid
    geld."""
    from sqlalchemy import func

    from ..db import GemeenteRegel, Listing, SessionLocal
    with SessionLocal() as s:
        rijen = (s.query(Listing.city, func.count())
                 .filter(Listing.is_demo.is_(False), Listing.city != "",
                         Listing.living_area >= MIN_M2)
                 .group_by(Listing.city)
                 .order_by(func.count().desc()).all())
        bekend = {r.city for r in s.query(GemeenteRegel).all()}
    # Onbekende gemeenten eerst; bekende worden alleen ververst als ze
    # verouderd zijn (dat beslist regels.check zelf).
    nieuw = [c.lower() for c, _ in rijen if c.lower() not in bekend]
    oud = [c.lower() for c, _ in rijen if c.lower() in bekend]
    return (nieuw + oud)[:limit]


def _topdeals(n: int) -> list[dict]:
    """De deals die de Criticus mag aanvallen: koop én veiling, geen projecten
    (die zijn per definitie indicatief)."""
    from ..scenarios import get_profile, top_listings
    params = get_profile(PROFIEL)
    uit: list[dict] = []
    for soort in ("koop", "veiling"):
        try:
            uit += top_listings(params, n=n, rank="risk", region="alle",
                                soort=soort)
        except Exception as e:
            print(f"[team] topdeals {soort} mislukt: {e}", flush=True)
    return uit


def run_team(max_lezen: int | None = None, top_n: int | None = None,
             forceer_regels: bool = False) -> dict:
    """Laat het team één ronde doen. Veilig om vaak aan te roepen."""
    if not agents_enabled():
        return {"status": "uit", "reden": "ANTHROPIC_API_KEY ontbreekt"}
    if budget_over() <= 0:
        return {"status": "budget_op", **status()}

    report: dict = {"status": "ok", "profiel": PROFIEL}

    # 1. Lezer — alles wat nog niet beoordeeld is
    try:
        ids = _te_lezen(max_lezen or MAX_LEZEN)
        report["lezer"] = (agent_lezer.lees_batch(ids) if ids
                           else {"gelezen": 0, "niets_te_doen": True})
    except Exception as e:
        report["lezer"] = {"fout": str(e)[:200]}
        print(f"[team] lezer: {traceback.format_exc()[:400]}", flush=True)

    # 2. Regelchecker — splitsbeleid per gemeente
    try:
        steden = _steden_van_kanshebbers(MAX_STEDEN)
        report["regelchecker"] = (agent_regels.check_steden(steden, forceer_regels)
                                  if steden else {"gecheckt": 0})
    except Exception as e:
        report["regelchecker"] = {"fout": str(e)[:200]}
        print(f"[team] regelchecker: {traceback.format_exc()[:400]}", flush=True)

    # 3. Scores bijwerken met wat de Lezer vond, zódat de Criticus de juiste
    #    topdeals te zien krijgt (en niet de lijst van vóór het lezen).
    try:
        from ..scoring import compute_scores
        report["_herscoord"] = compute_scores()
    except Exception as e:
        report["_score_fout"] = str(e)[:200]

    # 4. Criticus — alleen de kop van de lijst
    try:
        deals = _topdeals(top_n or TOP_N)
        report["criticus"] = (agent_criticus.beoordeel_deals(deals) if deals
                              else {"beoordeeld": 0})
    except Exception as e:
        report["criticus"] = {"fout": str(e)[:200]}
        print(f"[team] criticus: {traceback.format_exc()[:400]}", flush=True)

    # 5. Definitieve scores (kritiek verwerkt)
    try:
        from ..scoring import compute_scores
        report["_herscoord"] = compute_scores()
    except Exception as e:
        report["_score_fout"] = str(e)[:200]

    report["kosten"] = {"deze_ronde_usd": round(VERBRUIK["usd"], 4),
                        "over_vandaag_usd": round(budget_over(), 4)}
    try:
        from ..db import log_run
        log_run("agents", "ok",
                found=(report.get("lezer", {}) or {}).get("gelezen", 0),
                new=(report.get("criticus", {}) or {}).get("beoordeeld", 0),
                message=f"${report['kosten']['deze_ronde_usd']} verbruikt")
    except Exception:
        pass
    return report
