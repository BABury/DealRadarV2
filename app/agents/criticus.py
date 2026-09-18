"""Criticus — mag alleen tegenargumenten geven.

Een ranglijst optimaliseert op rekenwinst en die staat altijd het hoogst waar
de aannames het gunstigst uitvallen. Wat ontbreekt is iemand die zegt: dit
lijkt mooi omdat de data onvolledig is. Daarom krijgt deze agent één opdracht:
maak dit bod kapot. Wat hij hard maakt, gaat van de score af.

Hij ziet de doorgerekende cijfers, de feiten van de Lezer en het
gemeentebeleid. Hij mag de cijfers NIET herrekenen — alleen aanvallen.
"""
from __future__ import annotations

import datetime as dt
import json

from . import MODEL_DENKER, als_json, budget_over, kort, vraag_json
from . import regels as regelchecker

SYSTEM = """Je bent de kritische partner van een Nederlandse vastgoedbelegger. Hij
koopt panden om te splitsen in appartementen. Zijn systeem heeft dit object als
kans bestempeld. Jouw taak is het tegendeel bewijzen.

Zoek naar redenen waarom dit géén goede deal is:
- splitsen dat juridisch niet mag of jaren duurt (gemeentebeleid, VvE, erfpacht)
- verkoopprijs per appartement die op te weinig referenties rust
- verbouwkosten die bij dit bouwjaar/staat/monumentstatus veel hoger uitvallen
- een zittende huurder, huurbescherming of een veilingvoorwaarde
- een object dat alleen goedkoop lijkt doordat data ontbreekt (geen m², geen label)
- funderings-, asbest- of constructierisico bij oude panden
- parkeernorm, brandveiligheid of geluidseisen die splitsen duur maken

Regels:
- Je rekent NIETS opnieuw uit. Geen eigen bedragen of rendementen.
- Alleen bezwaren die je kunt onderbouwen met de gegeven informatie of met
  algemeen geldende Nederlandse regelgeving. Geen vage 'de markt kan dalen'.
- Ontbrekende informatie is zelf een bezwaar: benoem wat je mist.
- Wees streng maar eerlijk: is het bezwaar zwak, geef het dan ernst 'laag'.
- Antwoord in het Nederlands, kort en concreet."""

SCHEMA = {
    "type": "object",
    "properties": {
        "rode_vlaggen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "punt": {"type": "string", "description": "max 10 woorden"},
                    "ernst": {"type": "string", "enum": ["laag", "midden", "hoog"]},
                    "waarom": {"type": "string", "description": "max 25 woorden, onderbouwing"},
                },
                "required": ["punt", "ernst", "waarom"],
            },
            "description": "Maximaal 5, belangrijkste eerst",
        },
        "correctie": {"type": "integer",
                      "description": "Aftrek op de score, -30 (onacceptabel) tot 0 (niets gevonden)"},
        "advies": {"type": "string", "enum": ["doorgaan", "uitzoeken", "laten_lopen"]},
        "eerst_uitzoeken": {"type": "array", "items": {"type": "string"},
                            "description": "Max 3 vragen die je vóór een bod stelt aan makelaar of gemeente"},
        "samenvatting": {"type": "string",
                         "description": "Eén zin: het zwaarste bezwaar tegen deze deal"},
    },
    "required": ["rode_vlaggen", "correctie", "advies", "eerst_uitzoeken",
                 "samenvatting"],
}

# Cijfers die de criticus mag zien (niet herrekenen).
DEAL_VELDEN = ("address", "city", "neighbourhood", "source", "categorie",
               "price", "max_bod", "living_area", "price_m2", "build_year",
               "energy_label", "units", "split_status", "split_reden",
               "unit_m2", "best_strategie", "best_mid", "best_laag",
               "roi_laag_pct", "marge_pct", "split_premie", "split_premie_bron",
               "confidence", "flip_score", "days_on_market", "veiling",
               "auction_date")


def _prompt(deal: dict, listing: dict) -> str:
    cijfers = {k: deal.get(k) for k in DEAL_VELDEN if deal.get(k) not in (None, "")}
    lezer = listing.get("ai_bevinding") or {}
    if isinstance(lezer, str):
        try:
            lezer = json.loads(lezer)
        except ValueError:
            lezer = {}
    return (
        "DOORGEREKENDE KANS (door het systeem, niet herrekenen):\n"
        f"{json.dumps(cijfers, ensure_ascii=False, indent=1)}\n\n"
        f"SPLITSBELEID GEMEENTE:\n{regelchecker.voor_prompt(deal.get('city') or '')}\n\n"
        f"FEITEN UIT DE ADVERTENTIE (agent Lezer):\n{als_json(lezer) if lezer else '(nog niet gelezen)'}\n\n"
        f"ORIGINELE TEKST:\n{kort(listing.get('context'), 3500) or '(geen tekst)'}\n\n"
        "Val deze kans aan volgens je instructies."
    )


def _correctie(oordeel: dict) -> int:
    """Begrensd, en niet zwaarder dan de ernst die hij zelf noemt. Anders
    kan één streng antwoord een goede deal uit de lijst duwen."""
    c = int(oordeel.get("correctie") or 0)
    c = max(-30, min(0, c))
    ernsten = {v.get("ernst") for v in (oordeel.get("rode_vlaggen") or [])
               if isinstance(v, dict)}
    if not ernsten or ernsten == {"laag"}:
        c = max(c, -5)
    elif "hoog" not in ernsten:
        c = max(c, -15)
    return c


def beoordeel(deal: dict, listing: dict) -> dict:
    return vraag_json(agent="criticus", model=MODEL_DENKER, system=SYSTEM,
                      prompt=_prompt(deal, listing), schema=SCHEMA,
                      naam="kritiek", max_tokens=8000)


def sla_op(listing_id: int, oordeel: dict) -> dict:
    """Bewaart de kritiek naast het oordeel van de Lezer, zonder dat te wissen."""
    from . import noteer_toegepast
    from ..db import Listing, SessionLocal
    corr = _correctie(oordeel)
    voorgesteld = int(oordeel.get("correctie") or 0)
    ernsten = sorted({v.get("ernst") for v in (oordeel.get("rode_vlaggen") or [])
                      if isinstance(v, dict) and v.get("ernst")})
    noteer_toegepast({
        "correctie_voorgesteld": voorgesteld,
        "correctie_toegepast": corr,
        "reden_aanpassing": (
            "" if voorgesteld == corr else
            f"begrensd: bij ernst {', '.join(ernsten) or 'geen'} is de maximale aftrek "
            f"{'5' if not ernsten or ernsten == ['laag'] else '15' if 'hoog' not in ernsten else '30'}"),
        "advies": oordeel.get("advies"),
        "melding_telegram": "geblokkeerd" if oordeel.get("advies") == "laten_lopen" else "toegestaan",
    })
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if not row:
            return {"id": listing_id, "status": "verdwenen"}
        bev = {}
        if row.ai_bevinding:
            try:
                bev = json.loads(row.ai_bevinding)
            except ValueError:
                bev = {}
        bev["criticus"] = {**oordeel, "correctie": corr,
                           "op": dt.datetime.utcnow().isoformat(timespec="seconds")}
        row.ai_bevinding = json.dumps(bev, ensure_ascii=False)
        row.ai_advies = (oordeel.get("advies") or "")[:20]
        # Punten van Lezer en Criticus samen, begrensd zodat de rest van de
        # score (prijs/m², label, veiling) niet volledig wordt overschreeuwd.
        lezer_punten = int(bev.get("punten") or 0)
        row.ai_punten = max(-35, min(20, lezer_punten + corr))
        row.ai_checked = dt.datetime.utcnow()
        s.commit()
    return {"id": listing_id, "advies": row.ai_advies, "correctie": corr,
            "punten": row.ai_punten,
            "rode_vlaggen": len(oordeel.get("rode_vlaggen") or [])}


def beoordeel_deals(deals: list[dict]) -> dict:
    """Loopt de topdeals langs. Duurste agent, dus bewust een korte lijst."""
    from . import AgentGestopt, begin, klaar, onderwerp, stap, stop_gevraagd
    from ..db import Listing, SessionLocal

    gedaan, fouten = [], []
    begin("criticus", len(deals))
    for deal in deals:
        if stop_gevraagd():
            print("[criticus] gestopt op verzoek", flush=True)
            break
        if budget_over() <= 0:
            print("[criticus] dagbudget op — rest volgende run", flush=True)
            break
        lid = deal.get("id")
        if not lid:
            continue
        with SessionLocal() as s:
            row = s.get(Listing, lid)
            listing = row.to_dict() if row else None
        if not listing:
            continue
        try:
            with onderwerp(listing_id=lid, stad=(listing.get("city") or "").lower(),
                           onderwerp=listing.get("address") or f"object {lid}"):
                gedaan.append(sla_op(lid, beoordeel(deal, listing)))
            stap("criticus", f"{deal.get('address') or lid} ({deal.get('city') or ''})")
        except AgentGestopt:
            break
        except Exception as e:
            fouten.append({"id": lid, "fout": str(e)[:160]})
            stap("criticus", fout=True)
            if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                break
    klaar("criticus", f"{len(gedaan)} deals beoordeeld, {len(fouten)} fouten")
    return {"beoordeeld": len(gedaan), "fouten": len(fouten),
            "details": gedaan, "foutmeldingen": fouten[:5]}
