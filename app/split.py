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


def analyse(listing: dict, min_app_m2: float = 50, verkeer_pct: float = 10) -> dict:
    """Geeft {'units', 'status', 'zekerheid', 'unit_m2', 'reden'}."""
    area = float(listing.get("living_area") or 0)
    # Bewust zonder adres: anders telt elke 'Kerkstraat' als kerk.
    tekst = " ".join(str(listing.get(k) or "") for k in
                     ("property_type", "context")).lower()
    soort = str(listing.get("property_type") or "").lower()
    fl = _flags(listing)

    bruikbaar = area * (1 - verkeer_pct / 100)
    units = int(math.floor(bruikbaar / min_app_m2)) if (area and min_app_m2) else 0

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
    if "huurbeding ingeroepen" in tekst:
        return {"units": max(units, 1) if area else 0, "status": "nee", "zekerheid": 0.0,
                "unit_m2": area, "reden": "huurbeding ingeroepen — huurder blijft zitten"}

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

    return {"units": units, "status": status, "zekerheid": zeker,
            "unit_m2": round(bruikbaar / units) if units else area, "reden": reden}


def units_label(a: dict) -> str:
    if a["status"] == "nee":
        return ""
    pre = "" if a["status"] in ("vergunning", "genoemd") else "~"
    return f"{pre}{a['units']} app."
