"""Lezer — beoordeelt per object de tekst die de scraper niet begrijpt.

De scraper zet losse vlaggen op basis van woorden ('casco', 'te splitsen').
Dat mist context: "helaas niet te splitsen" zet nu dezelfde vlag als "eenvoudig
te splitsen". De Lezer kijkt naar de betekenis en levert feiten met een citaat
erbij, zodat jij kunt controleren waaróm hij iets vindt.

Hij mag GEEN bedragen of winsten noemen. Alleen: wat staat er, wat betekent
dat voor splitsen, en welke risico's horen erbij. Rekenen doet de code.
"""
from __future__ import annotations

import datetime as dt
import json

from . import MODEL_LEZER, budget_over, kort, vraag_json

SYSTEM = """Je bent vastgoedanalist voor een Nederlandse belegger. Zijn strategie:
een pand kopen en splitsen in meerdere zelfstandige appartementen, met zo min
mogelijk risico en moeite.

Jouw taak: lees de objectgegevens en haal er de FEITEN uit die bepalen of
splitsen kan. Je beoordeelt taal, geen cijfers.

Regels:
- Verzin niets. Staat iets er niet, dan is het 'onbekend'.
- Let op ontkenningen: "niet te splitsen" en "reeds gesplitst" zijn géén kans.
- Citeer bij elk hard oordeel de letterlijke woorden uit de tekst (bewijs).
- Noem NOOIT bedragen, winst of rendement. Dat rekent het systeem zelf.
- Een appartement in een VvE is niet verder splitsbaar; een grondgebonden pand
  met meerdere woonlagen of een aparte opgang juist wel.
- Een zittende huurder (huurbeding ingeroepen, verhuurde staat) is een
  dealbreaker: dan kun je niet leeg opleveren.
- Antwoord in het Nederlands, zakelijk en kort."""

SCHEMA = {
    "type": "object",
    "properties": {
        "splitsbaar": {"type": "string", "enum": ["ja", "nee", "onzeker"],
                       "description": "Kan dit pand volgens de tekst gesplitst worden?"},
        "max_appartementen": {"type": "integer",
                              "description": "Realistisch aantal zelfstandige woningen, 0 als onbekend"},
        "bewijs": {"type": "string",
                   "description": "Letterlijk citaat uit de tekst dat je oordeel onderbouwt, leeg als er niets staat"},
        "aparte_opgang": {"type": "string", "enum": ["ja", "nee", "onbekend"]},
        "al_gesplitst": {"type": "boolean",
                         "description": "Is het pand al gesplitst of al in appartementen verdeeld?"},
        "monument": {"type": "string",
                     "enum": ["rijksmonument", "gemeentelijk", "beschermd_gezicht", "nee", "onbekend"]},
        "verhuurd": {"type": "string", "enum": ["leeg", "verhuurd", "huurbeding", "onbekend"]},
        "staat": {"type": "string",
                  "enum": ["casco", "slecht", "matig", "goed", "gerenoveerd", "onbekend"]},
        "verbouwzwaarte": {"type": "string", "enum": ["licht", "middel", "zwaar", "onbekend"]},
        "risicos": {"type": "array", "items": {"type": "string"},
                    "description": "Concrete risico's uit de tekst, max 4, elk max 12 woorden"},
        "kansen": {"type": "array", "items": {"type": "string"},
                   "description": "Concrete pluspunten uit de tekst, max 3, elk max 12 woorden"},
        "punten": {"type": "integer",
                   "description": "Correctie op de score, -20 (slecht) tot +20 (uitstekend), 0 als de tekst niets toevoegt"},
        "samenvatting": {"type": "string",
                         "description": "Eén zin: wat is dit en kan splitsen, in het Nederlands"},
    },
    "required": ["splitsbaar", "max_appartementen", "bewijs", "aparte_opgang",
                 "al_gesplitst", "monument", "verhuurd", "staat",
                 "verbouwzwaarte", "risicos", "kansen", "punten", "samenvatting"],
}

# Waar de Lezer mee rekent: alleen wat de scraper echt heeft gezien.
VELDEN = ("source", "address", "city", "neighbourhood", "postcode",
          "property_type", "price", "living_area", "plot_area", "build_year",
          "rooms", "floors", "energy_label", "maintenance", "erfpacht",
          "auction_date", "published")


def _prompt(d: dict) -> str:
    feiten = {k: d.get(k) for k in VELDEN if d.get(k) not in (None, "", 0)}
    tekst = kort(d.get("context"), int(6000))
    return (f"OBJECTGEGEVENS (uit de bron):\n{json.dumps(feiten, ensure_ascii=False, indent=1)}\n\n"
            f"OMSCHRIJVING / VEILINGTEKST:\n{tekst or '(geen tekst beschikbaar)'}\n\n"
            "Beoordeel dit object volgens je instructies.")


def beoordeel(listing: dict) -> dict:
    """Eén object. Gooit AgentUit als de key mist of het budget op is."""
    return vraag_json(agent="lezer", model=MODEL_LEZER, system=SYSTEM,
                      prompt=_prompt(listing), schema=SCHEMA,
                      naam="beoordeling", max_tokens=1200)


def _punten(oordeel: dict) -> int:
    """De agent stelt punten voor; wij begrenzen ze en overrulen de gevallen
    waar we zelf al zeker van zijn. Zo kan één rare uitschieter de ranglijst
    niet kapen."""
    p = int(oordeel.get("punten") or 0)
    p = max(-20, min(20, p))
    if oordeel.get("verhuurd") == "huurbeding":
        p = min(p, -20)
    elif oordeel.get("verhuurd") == "verhuurd":
        p = min(p, -8)
    if oordeel.get("al_gesplitst"):
        p = min(p, -5)
    return p


def sla_op(listing_id: int, oordeel: dict) -> dict:
    """Schrijft het oordeel bij het object. Score volgt uit compute_scores()."""
    from . import noteer_toegepast
    from ..db import Listing, SessionLocal
    punten = _punten(oordeel)
    units = int(oordeel.get("max_appartementen") or 0)
    voorgesteld = int(oordeel.get("punten") or 0)
    noteer_toegepast({
        "punten_voorgesteld": voorgesteld,
        "punten_toegepast": punten,
        "reden_aanpassing": (
            "" if voorgesteld == punten else
            "huurbeding = altijd −20" if oordeel.get("verhuurd") == "huurbeding" else
            "verhuurd = hooguit −8" if oordeel.get("verhuurd") == "verhuurd" else
            "al gesplitst = hooguit −5" if oordeel.get("al_gesplitst") else
            "begrensd op −20..+20"),
        "splitsbaar": oordeel.get("splitsbaar"),
        "appartementen": units or None,
    })
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if not row:
            return {"id": listing_id, "status": "verdwenen"}
        row.ai_splits = (oordeel.get("splitsbaar") or "onzeker")[:10]
        row.ai_units = units or None
        row.ai_punten = punten
        row.ai_samenvatting = (oordeel.get("samenvatting") or "")[:600]
        row.ai_bevinding = json.dumps(oordeel, ensure_ascii=False)
        row.ai_checked = dt.datetime.utcnow()
        s.commit()
    return {"id": listing_id, "splitsbaar": row.ai_splits, "units": units,
            "punten": punten}


def lees_batch(listing_ids: list[int], max_objecten: int | None = None) -> dict:
    """Leest een reeks objecten. Stopt netjes zodra het budget op is, zodat
    een grote achterstand over meerdere runs wordt weggewerkt."""
    from ..db import Listing, SessionLocal

    from . import begin, klaar, onderwerp, stap

    gedaan, fouten, overgeslagen = [], [], 0
    ids = listing_ids[:max_objecten] if max_objecten else listing_ids
    begin("lezer", len(ids))
    for lid in ids:
        if budget_over() <= 0:
            overgeslagen = len(ids) - len(gedaan) - len(fouten)
            print(f"[lezer] dagbudget op — {overgeslagen} objecten volgende run",
                  flush=True)
            break
        with SessionLocal() as s:
            row = s.get(Listing, lid)
            d = row.to_dict() if row else None
        if not d:
            continue
        try:
            with onderwerp(listing_id=lid, stad=(d.get("city") or "").lower(),
                           onderwerp=d.get("address") or f"object {lid}"):
                gedaan.append(sla_op(lid, beoordeel(d)))
            stap("lezer", f"{d.get('address') or lid} ({d.get('city') or ''})")
        except Exception as e:
            fouten.append({"id": lid, "fout": str(e)[:160]})
            stap("lezer", fout=True)
            # Een kapotte tekst of time-out mag de rest niet tegenhouden,
            # maar bij een structurele fout (key ongeldig) stoppen we wel.
            if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                break
    klaar("lezer", f"{len(gedaan)} gelezen, {len(fouten)} fouten")
    return {"gelezen": len(gedaan), "fouten": len(fouten),
            "overgeslagen": overgeslagen, "details": gedaan[:50],
            "foutmeldingen": fouten[:5]}
