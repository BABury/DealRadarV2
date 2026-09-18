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

PROFIEL = os.getenv("AGENT_PROFIEL", os.getenv("ALERT_PROFILE", "bob"))
KEES_OPNIEUW_NA_DAGEN = 14     # een topdeal niet elke ronde opnieuw laten bekritiseren


def _instellingen() -> dict:
    from .instellingen import lees
    return lees()


def _kandidaten(limit: int | None = None) -> list[int]:
    """Objecten die Lotte mag lezen, belangrijkste eerst.

    Alleen wat voor jouw strategie telt:
      - in een focusstad (grote steden waar splitsen zin heeft)
      - binnen de m²-grenzen van je splitsprofiel
      - met tekst om te lezen (Funda-zoekkaartjes hebben geen omschrijving)
      - en (standaard) waar de rekensom al splitspotentie ziet — Lotte
        bevestigt of ontkracht dat, ze zoekt niet in het wilde weg.
    """
    from sqlalchemy import desc, func

    from ..db import Listing, SessionLocal
    from ..scoring import SPLIT_MIN_APP_M2, SPLIT_VERKEER_PCT
    from ..split import analyse as split_analyse
    from .instellingen import focus_varianten

    ins = _instellingen()
    with SessionLocal() as s:
        q = (s.query(Listing)
             .filter(Listing.is_demo.is_(False),
                     Listing.ai_checked.is_(None),
                     Listing.context.isnot(None), Listing.context != "",
                     func.lower(Listing.city).in_(sorted(focus_varianten(ins["steden"]))),
                     Listing.living_area >= ins["lotte_min_m2"]))
        if ins["lotte_max_m2"]:
            q = q.filter(Listing.living_area <= ins["lotte_max_m2"])
        rijen = q.order_by(desc(Listing.flip_score), desc(Listing.first_seen)).all()
        uit: list[int] = []
        for r in rijen:
            if ins["lotte_alleen_splitspotentie"]:
                sa = split_analyse(r.to_dict(), SPLIT_MIN_APP_M2, SPLIT_VERKEER_PCT)
                if sa["status"] == "nee":
                    continue
            uit.append(r.id)
            if limit and len(uit) >= limit:
                break
        return uit


def wachtrij() -> int:
    """Hoeveel objecten wachten nog op Lotte (zelfde voorwaarden als hierboven)."""
    return len(_kandidaten())


def _steden_van_kanshebbers(limit: int) -> list[str]:
    """Focussteden die Rik nog moet uitzoeken, drukste eerst.

    Alleen steden waar echt splitskandidaten liggen, en alleen als het beleid
    nog niet (geldig) bekend is — een stad die al is uitgezocht kost niets
    en neemt dus ook geen plek in de ronde in.
    """
    from sqlalchemy import func

    from ..db import GemeenteRegel, Listing, SessionLocal
    from .instellingen import focus_varianten, stad

    ins = _instellingen()
    with SessionLocal() as s:
        rijen = (s.query(Listing.city, func.count())
                 .filter(Listing.is_demo.is_(False),
                         func.lower(Listing.city).in_(sorted(focus_varianten(ins["steden"]))),
                         Listing.living_area >= ins["lotte_min_m2"],
                         Listing.living_area <= (ins["lotte_max_m2"] or 10 ** 9))
                 .group_by(Listing.city).all())
        regels = {r.city: r.to_dict() for r in s.query(GemeenteRegel).all()}
    per_stad: dict[str, int] = {}
    for c, n in rijen:
        per_stad[stad(c)] = per_stad.get(stad(c), 0) + n
    te_doen = [c for c, _ in sorted(per_stad.items(), key=lambda x: -x[1])
               if agent_regels._verouderd(regels.get(c))]
    return te_doen[:limit]


def _topdeals(n: int) -> list[dict]:
    """De deals die Kees mag aanvallen: koop én veiling in de focussteden,
    geen projecten, en niet opnieuw als hij ze onlangs al bekeek."""
    import datetime as dt
    import json as _json

    from ..db import Listing, SessionLocal
    from ..scenarios import get_profile, top_listings
    params = get_profile(PROFIEL)
    uit: list[dict] = []
    grens = dt.datetime.utcnow() - dt.timedelta(days=KEES_OPNIEUW_NA_DAGEN)
    for soort in ("koop", "veiling"):
        try:
            # top_listings geeft {"top": [...], "beoordeeld": n} terug
            lijst = (top_listings(params, n=max(n * 3, 5), rank="risk", region="focus",
                                  soort=soort) or {}).get("top", [])
        except Exception as e:
            print(f"[team] topdeals {soort} mislukt: {e}", flush=True)
            continue
        gekozen = 0
        with SessionLocal() as s:
            for d in lijst:
                row = s.get(Listing, d.get("id"))
                krit = {}
                if row and row.ai_bevinding:
                    try:
                        krit = _json.loads(row.ai_bevinding).get("criticus") or {}
                    except ValueError:
                        krit = {}
                op = krit.get("op")
                if op and dt.datetime.fromisoformat(op) > grens:
                    continue            # onlangs al bekritiseerd
                uit.append(d)
                gekozen += 1
                if gekozen >= n:
                    break
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
    ins = _instellingen()
    report: dict = {"status": "ok", "profiel": PROFIEL,
                    "focussteden": ins["steden"]}

    # 1. Lotte — alleen kandidaten in de focussteden
    try:
        if not ins["lotte_aan"]:
            report["lezer"] = {"gelezen": 0, "uit": True}
        else:
            ids = _kandidaten(max_lezen or ins["lotte_per_ronde"])
            report["lezer"] = (agent_lezer.lees_batch(ids) if ids
                               else {"gelezen": 0, "niets_te_doen": True})
    except Exception as e:
        report["lezer"] = {"fout": str(e)[:200]}
        print(f"[team] lezer: {traceback.format_exc()[:400]}", flush=True)

    # 2. Rik — alleen focussteden die nog niet (geldig) zijn uitgezocht
    try:
        if not ins["rik_aan"]:
            report["regelchecker"] = {"gecheckt": 0, "uit": True}
        else:
            steden = _steden_van_kanshebbers(ins["rik_steden_per_ronde"])
            report["regelchecker"] = (agent_regels.check_steden(steden, forceer_regels)
                                      if steden else {"gecheckt": 0, "niets_te_doen": True})
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

    # 4. Kees — alleen de kop van de lijst in de focussteden
    try:
        if not ins["kees_aan"]:
            report["criticus"] = {"beoordeeld": 0, "uit": True}
        else:
            deals = _topdeals(top_n or ins["kees_top_n"])
            report["criticus"] = (agent_criticus.beoordeel_deals(deals) if deals
                                  else {"beoordeeld": 0, "niets_te_doen": True})
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


# ── Op verzoek: één object laten uitzoeken ───────────────────────
# Het team doet niets uit zichzelf. Jij geeft de opdracht (dashboard of
# Telegram) en dan pakt het team precies dat object op:
#   Rik    zoekt het beleid van die stad op, als dat nog niet (geldig) bekend is
#   Lotte  leest het object (opnieuw, ook als ze het eerder las)
#   Kees   valt de doorgerekende deal aan
# Alles komt als één ronde in het logboek, met score voor → na.

def _deal_voor(listing: dict) -> dict:
    """De doorgerekende cijfers voor één object, in dezelfde vorm als een
    topdeal. Rekenen blijft het model; Kees krijgt alleen de uitkomst."""
    from ..benchmarks import BenchmarkMap
    from ..scenarios import city_medians_all, get_profile, scenario_table
    d = dict(listing)
    try:
        med = city_medians_all().get((d.get("city") or "").lower())
        tab = scenario_table(d, get_profile(PROFIEL), med, BenchmarkMap())
    except Exception as e:
        tab = {"error": str(e)[:120]}
    if "error" in tab:
        d["rekenmodel"] = f"niet door te rekenen: {tab['error']}"
        return d
    f, sa = tab.get("focus") or {}, tab.get("split") or {}
    d.update({
        "best_strategie": f.get("best_strategie"), "best_mid": f.get("best_mid"),
        "best_laag": f.get("best_laag"), "roi_laag_pct": f.get("roi_laag_pct"),
        "marge_pct": f.get("marge_pct"), "units": sa.get("units"),
        "split_status": sa.get("status"), "split_reden": sa.get("reden"),
        "unit_m2": sa.get("unit_m2"), "split_premie": sa.get("premie"),
        "split_premie_bron": sa.get("premie_bron"),
        "veiling": not (d.get("price") or 0) > 0,
    })
    return d


def onderzoek_object(listing_id: int, trigger: str = "knop") -> dict:
    """Laat het hele team één object uitzoeken. Geeft een samenvatting terug
    die ook naar Telegram kan."""
    if not agents_enabled():
        return {"status": "uit", "reden": "ANTHROPIC_API_KEY ontbreekt"}
    if budget_over() <= 0:
        return {"status": "budget_op", **status()}

    from . import onderwerp
    from ..db import (Listing, SessionLocal, acties_effect_zetten, gemeente_regel,
                      ronde_klaar, ronde_start)
    from ..scoring import compute_scores

    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if not row:
            return {"status": "niet_gevonden", "listing_id": listing_id}
        score_voor, adres, stad_ = row.flip_score, row.address, row.city

    ronde_id = ronde_start(trigger)
    usd_start = VERBRUIK["usd"]
    voor = _scores()
    rapport: dict = {"status": "ok", "listing_id": listing_id, "adres": adres, "stad": stad_}
    try:
        with onderwerp(ronde_id=ronde_id):
            # 1. Rik: beleid van de stad (alleen als het nog niet bekend is)
            try:
                if agent_regels._verouderd(gemeente_regel(stad_)):
                    rapport["regelchecker"] = agent_regels.check_steden([stad_])
                else:
                    rapport["regelchecker"] = {"gecheckt": 0, "al_bekend": True}
            except Exception as e:
                rapport["regelchecker"] = {"fout": str(e)[:200]}
            # 2. Lotte: altijd (opnieuw) lezen op verzoek
            with SessionLocal() as s:
                heeft_tekst = bool((s.get(Listing, listing_id).context or "").strip())
            rapport["lezer"] = (agent_lezer.lees_batch([listing_id]) if heeft_tekst
                                else {"gelezen": 0, "geen_tekst": True})
            compute_scores()
            # 3. Kees: de doorgerekende deal aanvallen
            with SessionLocal() as s:
                listing = s.get(Listing, listing_id).to_dict()
            deal = _deal_voor(listing)
            deal["id"] = listing_id
            rapport["criticus"] = agent_criticus.beoordeel_deals([deal])
            compute_scores()
        if any((rapport.get(n) or {}).get("fouten") or (rapport.get(n) or {}).get("fout")
               for n in ("lezer", "regelchecker", "criticus")):
            rapport["status"] = "deels"
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
        rapport["kosten_usd"] = round(VERBRUIK["usd"] - usd_start, 4)
        rapport["ronde_id"] = ronde_id
        ronde_klaar(ronde_id, rapport.get("status", "error"), rapport,
                    rapport["kosten_usd"], wijz)

    # Samenvatting voor dashboard en Telegram
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        bev = {}
        try:
            import json as _json
            bev = _json.loads(row.ai_bevinding) if row.ai_bevinding else {}
        except ValueError:
            bev = {}
        rapport.update({
            "score_voor": score_voor, "score_na": row.flip_score,
            "lotte": {"splitsbaar": row.ai_splits, "units": row.ai_units,
                      "samenvatting": row.ai_samenvatting,
                      "risicos": bev.get("risicos") or []},
            "kees": {"advies": row.ai_advies,
                     **{k: (bev.get("criticus") or {}).get(k)
                        for k in ("samenvatting", "rode_vlaggen", "eerst_uitzoeken")}},
            "rik": {k: (gemeente_regel(stad_) or {}).get(k)
                    for k in ("toegestaan", "samenvatting", "zekerheid")},
        })
    return rapport
