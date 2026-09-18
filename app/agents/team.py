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

    Voorwaarde: er moet tekst zijn (omschrijving of veilingtekst) én het object
    moet groot genoeg zijn om te splitsen. Zoekkaartjes van Funda hebben geen
    omschrijving; die lezen kost geld en levert niets op.
    """
    from sqlalchemy import desc, or_

    from ..db import Listing, SessionLocal
    with SessionLocal() as s:
        q = (s.query(Listing.id)
             .filter(Listing.is_demo.is_(False),
                     Listing.ai_checked.is_(None),
                     or_(Listing.living_area >= MIN_M2,
                         Listing.living_area.is_(None)),
                     Listing.context.isnot(None), Listing.context != "")
             .order_by(desc(Listing.flip_score), desc(Listing.first_seen))
             .limit(limit))
        return [r[0] for r in q.all()]


def wachtrij() -> int:
    """Hoeveel objecten wachten nog op de Lezer? Zelfde voorwaarden als
    _te_lezen, zodat het getal in het dashboard klopt met wat er gebeurt."""
    from sqlalchemy import func, or_

    from ..db import Listing, SessionLocal
    with SessionLocal() as s:
        return (s.query(func.count(Listing.id))
                .filter(Listing.is_demo.is_(False),
                        Listing.ai_checked.is_(None),
                        or_(Listing.living_area >= MIN_M2,
                            Listing.living_area.is_(None)),
                        Listing.context.isnot(None), Listing.context != "")
                .scalar() or 0)


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
            # top_listings geeft {"top": [...], "beoordeeld": n} terug
            uit += (top_listings(params, n=n, rank="risk", region="alle",
                                 soort=soort) or {}).get("top", [])
        except Exception as e:
            print(f"[team] topdeals {soort} mislukt: {e}", flush=True)
    return uit


def _scores() -> dict[int, dict]:
    """Stand van alle objecten: score + wat het team erover zei. Vóór en na
    een ronde vergeleken = precies zien wat het team veranderde."""
    from ..db import Listing, SessionLocal
    with SessionLocal() as s:
        return {r.id: {"score": r.flip_score, "splits": r.ai_splits or "",
                       "advies": r.ai_advies or "", "punten": r.ai_punten,
                       "adres": r.address, "stad": r.city}
                for r in s.query(Listing.id, Listing.flip_score, Listing.ai_splits,
                                 Listing.ai_advies, Listing.ai_punten,
                                 Listing.address, Listing.city).all()}


def _wijzigingen(voor: dict, na: dict) -> list[dict]:
    uit = []
    for lid, n in na.items():
        v = voor.get(lid) or {}
        if (v.get("score") != n["score"] or v.get("splits") != n["splits"]
                or v.get("advies") != n["advies"]):
            uit.append({"listing_id": lid, "adres": n["adres"], "stad": n["stad"],
                        "score_voor": v.get("score"), "score_na": n["score"],
                        "verschil": (n["score"] or 0) - (v.get("score") or 0),
                        "splits_voor": v.get("splits") or "", "splits_na": n["splits"],
                        "advies_voor": v.get("advies") or "", "advies_na": n["advies"]})
    uit.sort(key=lambda w: -abs(w["verschil"]))
    return uit


def _met_oorzaak(ronde_id: int, wijz: list[dict]) -> list[dict]:
    """Welke scorewijziging komt door het team, en door wie?

    Alleen wat een agent in deze ronde met succes deed telt als zijn effect.
    Een score die verschuift doordat de markt of een nieuw object herberekend
    werd, is niet het werk van het team en wordt daar niet aan toegeschreven.
    """
    from ..db import AgentActie, SessionLocal
    with SessionLocal() as s:
        ok = (s.query(AgentActie.agent, AgentActie.listing_id, AgentActie.stad, AgentActie.stap)
              .filter(AgentActie.ronde_id == ronde_id, AgentActie.status == "ok").all())
    gelezen = {lid for a, lid, _, _ in ok if a == "lezer" and lid}
    bekritiseerd = {lid for a, lid, _, _ in ok if a == "criticus" and lid}
    beleid = {(stad or "").lower() for a, _, stad, stap in ok
              if a == "regelchecker" and stap == "beleid"}
    uit = []
    for w in wijz:
        wie = [n for n, hit in (("lotte", w["listing_id"] in gelezen),
                                ("kees", w["listing_id"] in bekritiseerd)) if hit]
        if wie:
            w["oorzaak"] = "+".join(wie)
        elif (w.get("stad") or "").lower() in beleid:
            w["oorzaak"] = "rik"            # nieuw gemeentebeleid raakt de hele stad
        else:
            continue                        # herberekening, niet het team
        uit.append(w)
    return uit


def run_team(max_lezen: int | None = None, top_n: int | None = None,
             forceer_regels: bool = False, trigger: str = "schema") -> dict:
    """Laat het team één ronde doen. Veilig om vaak aan te roepen.

    Elke ronde krijgt een nummer. Alle modelaanroepen worden daaronder in het
    logboek gezet, en aan het eind wordt vastgelegd welke objecten een andere
    score kregen — met de score ervoor en erna.
    """
    if not agents_enabled():
        return {"status": "uit", "reden": "ANTHROPIC_API_KEY ontbreekt"}
    if budget_over() <= 0:
        return {"status": "budget_op", **status()}

    from . import onderwerp
    from ..db import acties_effect_zetten, ronde_klaar, ronde_start

    ronde_id = ronde_start(trigger)
    usd_start = VERBRUIK["usd"]
    voor = _scores()
    report: dict = {"status": "error"}
    try:
        with onderwerp(ronde_id=ronde_id):
            report = _ronde(max_lezen, top_n, forceer_regels, usd_start)
        report["ronde_id"] = ronde_id
        return report
    finally:
        wijz: list[dict] = []
        try:
            wijz = _met_oorzaak(ronde_id, _wijzigingen(voor, _scores()))
            for w in wijz:
                if w["oorzaak"] in ("lotte", "kees", "lotte+kees"):
                    acties_effect_zetten(ronde_id, w["listing_id"], {
                        k: w[k] for k in ("score_voor", "score_na", "verschil",
                                          "splits_voor", "splits_na",
                                          "advies_voor", "advies_na")})
        except Exception as e:
            print(f"[team] wijzigingen niet vastgelegd: {e}", flush=True)
        ronde_klaar(ronde_id, report.get("status", "error"), report,
                    round(VERBRUIK["usd"] - usd_start, 4), wijz)


def _ronde(max_lezen, top_n, forceer_regels, usd_start: float) -> dict:
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

    if any((report.get(n) or {}).get("fouten") or (report.get(n) or {}).get("fout")
           for n in ("lezer", "regelchecker", "criticus")):
        report["status"] = "deels"
    report["kosten"] = {"deze_ronde_usd": round(VERBRUIK["usd"] - usd_start, 4),
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
