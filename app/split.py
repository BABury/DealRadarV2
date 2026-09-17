"""Splitsanalyse: hoeveel appartementen passen erin, en hoe zeker is dat?

Waarom dit apart staat: splitsen is de grootste waardesprong, maar voorheen
telde het alleen mee als de makelaar letterlijk 'splitsingsvergunning' in de
advertentie zette. Een herenhuis van 200 m² dat prima te splitsen is, werd
dan nooit als splitskans gezien.

Zekerheid (van hoog naar laag) — bepaalt hoe zwaar het meeweegt in de ranking:
  vergunning   splitsingsvergunning aanwezig
  genoemd      advertentie noemt splitsen / meerdere woningen expliciet
  potentieel   alleen berekend uit oppervlak en type — gemeente moet nog akkoord

LET OP: 'potentieel' is een fysieke inschatting, geen vergunning. Gemeenten
hanteren eigen regels (minimale woninggrootte, splitsingsverboden per wijk,
parkeernormen). Check die altijd vóór je biedt.
"""
from __future__ import annotations

import math
import re

# Woningtypes die zich niet (realistisch) laten splitsen: al een appartement
# binnen een VvE, of een object zonder vaste fundering.
NIET_SPLITSBAAR = [
    "appartement", "flat", "galerij", "portiek", "penthouse", "studio",
    "recreatiewoning", "woonboot", "woonwagen", "stacaravan", "chalet",
]

# Types die juist van nature geschikt zijn (per verdieping of per vleugel).
KANSRIJK = [
    "herenhuis", "grachtenpand", "woonboerderij", "villa", "landhuis",
    "vrijstaand", "bovenwoning", "winkel met bovenwoning", "pastorie",
    "transformatieobject", "voormalig politiebureau", "voormalige school",
    "kerk", "klooster", "kantoor", "praktijk",
]

# Tekst die zegt dat splitsen al (deels) geregeld of voorzien is.
GENOEMD = [
    "te splitsen", "splitsen in", "gesplitst worden", "splitsingsmogelijk",
    "twee woningen", "2 woningen", "drie woningen", "3 woningen",
    "meerdere woningen", "meerdere appartementen", "twee appartementen",
    "2 appartementen", "eigen opgang", "aparte opgang", "twee voordeuren",
]

ZEKERHEID = {"vergunning": 1.0, "genoemd": 0.93, "potentieel": 0.85, "nee": 0.0}


def _flags(listing: dict) -> dict:
    """Werkt zowel met to_dict() ('flags': {...}) als met scraper-dicts (flag_*)."""
    f = listing.get("flags")
    if isinstance(f, dict):
        return f
    return {k[5:]: v for k, v in listing.items() if k.startswith("flag_")}


def _gemeente(city: str) -> tuple[float, str, float | None]:
    """(factor, beleid, minimale woning-m²) van de Regelchecker.
    Zonder agents of zonder key: neutraal, dus precies zoals voorheen."""
    try:
        from .agents import regels
        f, beleid = regels.factor(city)
        mm = regels.regel(city).get("min_woning_m2")
        return f, beleid, (float(mm) if isinstance(mm, (int, float)) and mm > 0 else None)
    except Exception:
        return 1.0, "onbekend", None


def analyse(listing: dict, min_app_m2: float = 50, verkeer_pct: float = 10) -> dict:
    """Geeft {'units', 'status', 'zekerheid', 'unit_m2', 'reden'}."""
    area = float(listing.get("living_area") or 0)
    # Bewust zonder adres: anders telt elke 'Kerkstraat' als kerk.
    tekst = " ".join(str(listing.get(k) or "") for k in
                     ("property_type", "context")).lower()
    soort = str(listing.get("property_type") or "").lower()
    fl = _flags(listing)

    # Gemeentebeleid gaat vóór onze aanname: schrijft de gemeente een grotere
    # minimale woning voor, dan passen er minder appartementen in.
    gem_factor, beleid, gem_min = _gemeente(str(listing.get("city") or ""))
    if gem_min and gem_min > min_app_m2:
        min_app_m2 = gem_min

    bruikbaar = area * (1 - verkeer_pct / 100)
    units = int(math.floor(bruikbaar / min_app_m2)) if (area and min_app_m2) else 0

    # Wat de Lezer in de tekst vond (leeg zonder agents)
    ai = str(listing.get("ai_splits") or "").lower()
    ai_units = listing.get("ai_units")
    ai_bev = listing.get("ai_bevinding") if isinstance(listing.get("ai_bevinding"), dict) else {}
    ai_bewijs = str(ai_bev.get("bewijs") or "").strip()

    # 1. Expliciete signalen gaan boven de berekening
    if fl.get("splitsvergunning"):
        status = "vergunning"
    elif fl.get("splits_bouwkundig") or fl.get("splits_kadastraal") \
            or any(g in tekst for g in GENOEMD):
        status = "genoemd"
    else:
        status = None

    ongeschikt = any(n in soort for n in NIET_SPLITSBAAR)
    kansrijk = any(k in tekst for k in KANSRIJK)

    # Huurbeding ingeroepen (veiling): de huurder blijft na de koop wonen.
    # Dan kun je niet leeg verbouwen of splitsen — dat gaat vóór alles.
    if "huurbeding ingeroepen" in tekst or ai_bev.get("verhuurd") == "huurbeding":
        return {"units": max(units, 1) if area else 0, "status": "nee", "zekerheid": 0.0,
                "unit_m2": area, "reden": "huurbeding ingeroepen — huurder blijft zitten"}

    # De Lezer heeft de tekst gelezen en zegt: dit kan niet gesplitst worden
    # (al gesplitst, verhuurd, VvE-appartement, expliciet uitgesloten). Dat
    # weegt zwaarder dan onze woordenlijst, want die ziet ontkenningen niet.
    if ai == "nee":
        reden = ai_bev.get("samenvatting") or "agent Lezer: niet splitsbaar volgens de tekst"
        return {"units": max(units, 1) if area else 0, "status": "nee",
                "zekerheid": 0.0, "unit_m2": area, "reden": str(reden)[:200]}

    # Lezer vond bewijs in de tekst dat splitsen kan -> harder dan 'potentieel'
    if ai == "ja" and status is None:
        status = "genoemd" if ai_bewijs else "potentieel"
    if isinstance(ai_units, int) and ai_units >= 2 and units:
        # De Lezer kent de indeling (woonlagen, opgang); is hij voorzichtiger
        # dan de rekensom op oppervlak, dan volgen we hem.
        units = min(units, ai_units) if ai_units < units else units

    if status:
        # De advertentie zegt dat het kan: dan minstens 2 eenheden
        units = max(units, 2)
    elif ongeschikt:
        return {"units": 1 if area else 0, "status": "nee", "zekerheid": 0.0,
                "unit_m2": area, "reden": f"type '{soort}' laat zich niet splitsen"}
    elif units >= 2:
        status = "potentieel"
    else:
        return {"units": max(units, 1) if area else 0, "status": "nee",
                "zekerheid": 0.0, "unit_m2": area,
                "reden": (f"{area:.0f} m² is te klein voor 2× {min_app_m2:.0f} m²"
                          if area else "woonoppervlak onbekend")}

    # Woonlagen: 3 lagen = natuurlijke splitsing per verdieping
    lagen = listing.get("floors")
    extra = []
    if kansrijk:
        extra.append("geschikt type")
    if lagen and int(lagen) >= 2:
        extra.append(f"{int(lagen)} woonlagen")

    reden = {"vergunning": "splitsingsvergunning aanwezig",
             "genoemd": "splitsen genoemd in advertentie",
             "potentieel": f"{area:.0f} m² → {units}× ±{bruikbaar / units:.0f} m²"}[status]
    if extra:
        reden += " · " + ", ".join(extra)

    zeker = ZEKERHEID[status]
    # Potentieel mét geschikt type is iets zekerder dan puur op oppervlak
    if status == "potentieel" and kansrijk:
        zeker = 0.9

    # Gemeentebeleid is de laatste zeef. Een vergunning die er al ligt telt
    # vol; alles wat de gemeente nog moet goedkeuren zakt mee met het beleid.
    if status != "vergunning" and gem_factor < 1.0:
        zeker = round(zeker * gem_factor, 3)
        reden += f" · gemeente: splitsen {beleid.replace('_', ' ')}"

    return {"units": units, "status": status, "zekerheid": zeker,
            "unit_m2": round(bruikbaar / units) if units else area,
            "reden": reden, "gemeente_beleid": beleid}


def units_label(a: dict) -> str:
    if a["status"] == "nee":
        return ""
    pre = "" if a["status"] in ("vergunning", "genoemd") else "~"
    return f"{pre}{a['units']} app."
