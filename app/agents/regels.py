"""Regelchecker — mag je in déze gemeente eigenlijk wel splitsen?

Dit is het gat in de oude DealRadar. Een pand van 200 m² leverde 'splitspotentie
~3 app.' op, ook in een stad waar splitsen in de praktijk verboden is. Amsterdam
en Utrecht hebben strenge woningvormings- en splitsingsregels, Rotterdam is veel
ruimer. Datzelfde pand is dus in de ene stad een deal en in de andere een val.

Beleid verandert een paar keer per jaar, dus we zoeken het één keer per gemeente
op en bewaren het (standaard 30 dagen geldig). Zoeken gebeurt met websearch;
zonder internet zakt de zekerheid naar 'laag' en dat is in het dashboard te zien.
"""
from __future__ import annotations

import datetime as dt
import os

from . import MODEL_DENKER, als_json, onderzoek, vraag_json

GELDIG_DAGEN = int(os.getenv("AGENT_REGELS_DAGEN", "30"))

SYSTEM_ZOEK = """Je bent jurist ruimtelijke ordening. Je zoekt het actuele beleid
van één Nederlandse gemeente op over het SPLITSEN van een woning in meerdere
zelfstandige woningen (woningsplitsing) en over woningvorming/kamerverhuur.

Zoek specifiek naar:
- huisvestingsverordening of splitsingsverordening van de gemeente
- of een splitsingsvergunning of omzettingsvergunning nodig is
- minimale oppervlakte per zelfstandige woning (m²)
- quota, wijkverboden of een 'nee, tenzij'-beleid
- parkeernorm bij extra woningen
- recente wijzigingen (noem het jaartal)

Gebruik alleen bronnen van de gemeente zelf, overheid.nl, lokaleregelgeving.
overheid.nl of serieuze vakmedia. Geef per bevinding de bron. Zeg het eerlijk
als je iets niet vindt."""

SYSTEM_JSON = """Zet het onderzoeksverslag om in het gevraagde formaat.
Niets toevoegen wat er niet staat. Als het verslag onzeker is, zet zekerheid
op 'laag'. Antwoord in het Nederlands."""

SCHEMA = {
    "type": "object",
    "properties": {
        "toegestaan": {"type": "string",
                       "enum": ["ja", "ja_met_vergunning", "beperkt", "nee", "onbekend"],
                       "description": "beperkt = mag alleen in delen van de stad of onder strenge voorwaarden"},
        "vergunning_nodig": {"type": "boolean"},
        "min_woning_m2": {"type": ["number", "null"],
                          "description": "minimale oppervlakte per zelfstandige woning, null als onbekend"},
        "quota_of_verboden": {"type": "string",
                              "description": "korte omschrijving van quota, wijkverboden of 'nee tenzij', leeg als niet van toepassing"},
        "parkeernorm": {"type": "string", "description": "kort, leeg als onbekend"},
        "let_op": {"type": "array", "items": {"type": "string"},
                   "description": "max 4 waarschuwingen, elk max 15 woorden"},
        "zekerheid": {"type": "string", "enum": ["hoog", "midden", "laag"]},
        "samenvatting": {"type": "string",
                         "description": "Twee zinnen: mag splitsen hier, en wat de belangrijkste voorwaarde is"},
        "peiljaar": {"type": "string", "description": "jaartal van het gevonden beleid, leeg als onbekend"},
    },
    "required": ["toegestaan", "vergunning_nodig", "min_woning_m2",
                 "quota_of_verboden", "parkeernorm", "let_op", "zekerheid",
                 "samenvatting", "peiljaar"],
}

# Beleid is streng in de grote steden; dit is puur de reden waarom deze agent
# bestaat, niet een antwoord. De agent zoekt het echte beleid op.
VERWACHT_STRENG = {"amsterdam", "utrecht", "den-haag", "den haag", "haarlem",
                   "leiden", "groningen", "nijmegen", "delft"}


def _verouderd(regel: dict | None) -> bool:
    if not regel or not regel.get("updated"):
        return True
    try:
        d = dt.datetime.fromisoformat(str(regel["updated"]).replace("Z", ""))
    except ValueError:
        return True
    return (dt.datetime.utcnow() - d).days >= GELDIG_DAGEN


def check(city: str, forceer: bool = False) -> dict:
    """Beleid van één gemeente. Uit de database als het nog geldig is."""
    from ..db import gemeente_regel
    vergeet_cache()

    stad = (city or "").strip().lower()
    if not stad:
        return {}
    bestaand = gemeente_regel(stad)
    if bestaand and not forceer and not _verouderd(bestaand):
        return {**bestaand, "uit_cache": True}

    from . import noteer_toegepast, onderwerp
    net = stad.replace("-", " ").title()
    with onderwerp(stad=stad, onderwerp=net, listing_id=None):
        return _check_zoek(stad, net, bestaand, noteer_toegepast)


def _check_zoek(stad: str, net: str, bestaand: dict | None, noteer_toegepast) -> dict:
    from ..db import gemeente_regel_opslaan
    verslag, bronnen = onderzoek(
        agent="regelchecker", model=MODEL_DENKER, system=SYSTEM_ZOEK,
        prompt=(f"Gemeente: {net}\n\nZoek het actuele beleid voor woningsplitsing "
                f"in {net} op. Mag je daar een woning splitsen in meerdere "
                f"zelfstandige appartementen, en onder welke voorwaarden?"),
        max_uses=int(os.getenv("AGENT_WEB_USES", "6")))

    data = vraag_json(agent="regelchecker", model=MODEL_DENKER,
                      system=SYSTEM_JSON, schema=SCHEMA, naam="beleid",
                      prompt=f"Gemeente: {net}\n\nONDERZOEKSVERSLAG:\n{verslag}",
                      max_tokens=8000)

    opslaan = {
        "toegestaan": data.get("toegestaan"),
        "vergunning_nodig": data.get("vergunning_nodig", True),
        "min_woning_m2": data.get("min_woning_m2"),
        "zekerheid": data.get("zekerheid"),
        "samenvatting": data.get("samenvatting"),
        "details": {k: data.get(k) for k in
                    ("quota_of_verboden", "parkeernorm", "let_op", "peiljaar")},
        "bronnen": bronnen[:8],
    }
    # Wat verandert er door dit beleid? Vorige stand naast de nieuwe, plus
    # de rem op splitszekerheid die de code eruit afleidt.
    nieuw_factor = {"ja": 1.0, "ja_met_vergunning": 0.9, "beperkt": 0.65,
                    "nee": 0.25}.get(opslaan["toegestaan"] or "onbekend", 0.85)
    noteer_toegepast({
        "beleid_was": (bestaand or {}).get("toegestaan") or "niet uitgezocht",
        "beleid_nu": opslaan["toegestaan"],
        "zekerheidsfactor_splitsen": nieuw_factor,
        "min_woning_m2": opslaan["min_woning_m2"],
        "bronnen_bewaard": len(opslaan["bronnen"]),
        "geldig_tot": (dt.datetime.utcnow() + dt.timedelta(days=GELDIG_DAGEN)).date().isoformat(),
    })
    return {**gemeente_regel_opslaan(stad, opslaan), "uit_cache": False}


def check_steden(steden: list[str], forceer: bool = False) -> dict:
    """Werkt een lijst gemeenten af; stopt zodra het budget op is."""
    from . import begin, budget_over, klaar, stap

    gedaan, fouten = [], []
    lijst = list(dict.fromkeys(s.strip().lower() for s in steden if s and s.strip()))
    begin("regelchecker", len(lijst))
    for stad in lijst:
        if budget_over() <= 0:
            print("[regelchecker] dagbudget op — rest volgende run", flush=True)
            break
        try:
            r = check(stad, forceer=forceer)
            if r:
                gedaan.append({"stad": stad, "toegestaan": r.get("toegestaan"),
                               "uit_cache": r.get("uit_cache"),
                               "zekerheid": r.get("zekerheid")})
            stap("regelchecker", stad)
        except Exception as e:
            fouten.append({"stad": stad, "fout": str(e)[:160]})
            stap("regelchecker", stad, fout=True)
    klaar("regelchecker", f"{len(gedaan)} gemeenten, {len(fouten)} fouten")
    return {"gecheckt": len(gedaan), "fouten": len(fouten),
            "steden": gedaan, "foutmeldingen": fouten[:5]}


def factor(city: str) -> tuple[float, str]:
    """Hoe zwaar mag splitswinst meewegen in deze gemeente?

    Geen verzonnen precisie: dit is een rem op de zekerheid, geen kansberekening.
      ja                 1.00  beleid staat het toe
      ja_met_vergunning  0.90  vergunning is een formaliteit met doorlooptijd
      beperkt            0.65  mag alleen in delen van de stad / nee-tenzij
      nee                0.25  vrijwel zeker geen splitsvergunning
      onbekend           0.85  uitgezocht, maar niet te achterhalen

    Nog niet uitgezocht = 1.00. Zonder de Regelchecker werkt de app dus
    precies zoals voorheen; hij mag niets stiller maken dan het al was.
    """
    r = _regel_of_leeg(city)
    if not r:
        return 1.0, "niet_uitgezocht"
    t = (r.get("toegestaan") or "onbekend")
    f = {"ja": 1.0, "ja_met_vergunning": 0.9, "beperkt": 0.65,
         "nee": 0.25}.get(t, 0.85)
    if r.get("zekerheid") == "laag" and t in ("nee", "beperkt"):
        f = min(1.0, f + 0.1)          # onzeker slecht nieuws weegt minder zwaar
    return f, t


def min_m2(city: str, standaard: float) -> float:
    """Gemeentelijke minimale woninggrootte gaat vóór onze aanname."""
    r = _regel_of_leeg(city)
    mm = r.get("min_woning_m2")
    return float(mm) if isinstance(mm, (int, float)) and mm > 0 else standaard


# Kleine cache: compute_scores() loopt over honderden objecten en zou anders
# per object de database bevragen voor dezelfde handvol gemeenten.
_CACHE: dict = {"tijd": 0.0, "map": {}}
_CACHE_SEC = 300


def _alle_regels() -> dict:
    import time
    if time.time() - _CACHE["tijd"] > _CACHE_SEC:
        try:
            from ..db import gemeente_regels_alle
            _CACHE["map"] = {r["city"]: r for r in gemeente_regels_alle()}
        except Exception as e:
            print(f"[regelchecker] cache niet geladen: {e}", flush=True)
        _CACHE["tijd"] = time.time()
    return _CACHE["map"]


def vergeet_cache() -> None:
    _CACHE["tijd"] = 0.0


def _regel_of_leeg(city: str) -> dict:
    return _alle_regels().get((city or "").strip().lower(), {})


def regel(city: str) -> dict:
    """Publieke lezer: het bewaarde beleid van deze gemeente (of leeg)."""
    return _regel_of_leeg(city)


def samenvatting(city: str) -> str:
    return _regel_of_leeg(city).get("samenvatting") or ""


def voor_prompt(city: str) -> str:
    """Compacte beleidsregel om aan een andere agent mee te geven."""
    r = _regel_of_leeg(city)
    if not r:
        return "beleid van deze gemeente is nog niet uitgezocht"
    return als_json({k: r.get(k) for k in
                     ("toegestaan", "vergunning_nodig", "min_woning_m2",
                      "zekerheid", "samenvatting")})
