"""Filter: alleen woningen (en panden die woning kúnnen worden).

Aanleiding: veilingbronnen leveren van alles — pachtgronden, bospercelen,
benzinestations, los- en laadplaatsen en zelfs administratieve publicaties.
Dat vervuilt de database en de ranglijst.

Twee filters:
  is_residential()  woning, of een pand dat naar wonen getransformeerd wordt
  is_usable()       genoeg data om iets mee te kunnen (prijs of oppervlak)

Uitzetten kan met RESIDENTIAL_ONLY=0 (dan komt alles binnen).
"""
from __future__ import annotations

import os
import re

# Woningtypes — dit is waar je in belegt.
WONING = [
    "woning", "woonhuis", "huis", "appartement", "villa", "herenhuis",
    "bovenwoning", "benedenwoning", "tussenwoning", "hoekwoning",
    "eengezinswoning", "twee-onder-een-kap", "twéé-onder-een-kap",
    "vrijstaand", "woonboerderij", "kluswoning", "portiekwoning",
    "maisonnette", "studio", "penthouse", "landhuis", "bungalow",
    "stadswoning", "grachtenpand", "woonruimte", "starterswoning",
]

# Objecttypen die als transformatiekans gelden — dit staat in het TYPE zelf,
# dus geen giswerk uit de omschrijving.
TRANSFORMATIE_TYPES = [
    "transformatieobject", "voormalig politiebureau", "voormalige school",
    "voormalig schoolgebouw", "klooster", "kerkgebouw", "pastorie",
]

# Alleen als de omschrijving expliciet naar WONEN wijst telt herontwikkeling
# mee. Anders glipt er van alles door ("herontwikkeling attractiepark").
NAAR_WONEN = [
    "naar woning", "tot woning", "naar wonen", "woonbestemming",
    "naar appartement", "tot appartement", "woningbouw", "woningen mogelijk",
    "transformatie naar", "herbestemming naar wonen", "woonfunctie",
]

# Duidelijk géén woning — hier wil je niet in beleggen.
UITSLUITEN = [
    "benzinestation", "bedrijventerrein", "bedrijfsruimte", "bedrijfshal",
    "bedrijfsobject", "bedrijfspand", "loods", "opslag", "magazijn",
    "winkelruimte", "winkelcentrum", "horeca", "restaurant", "cafe", "café",
    "hotel", "kantoorruimte", "kantoorpand", "kantoor-", "praktijkruimte",
    "garagebox", "parkeerplaats", "parkeergarage", "vakantiepark", "camping",
    "attractiepark", "recreatiepark", "sportinstelling", "zorginstelling",
    "agrarisch", "pachtgrond", "landbouwgrond", "weiland", "bosperceel",
    "bospercelen", "perceel grond", "percelen grond", "bouwgrond",
    "los- en laadplaats", "laadplaats", "ligplaats", "volkstuin",
    "windturbine", "zendmast", "grond gelegen", "diverse publicaties",
    "diverse voornemens", "ingebruikgeving", "verhuur & ingebruikgeving",
    "jachthaven", "manege", "tankstation", "distributiecentrum",
]


def _norm(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def residential_only() -> bool:
    return os.getenv("RESIDENTIAL_ONLY", "1") not in ("0", "false", "no")


def is_residential(item: dict) -> bool:
    """True als dit een woning is, of een pand dat aantoonbaar wóning wordt."""
    soort = _norm(item.get("property_type"))
    tekst = _norm(item.get("property_type"), item.get("address"),
                  item.get("context"))
    if not tekst.strip():
        return False                      # geen enkele aanwijzing -> weglaten

    # 1. Het objecttype zelf is een erkende transformatiekans
    if any(t in soort for t in TRANSFORMATIE_TYPES):
        return True

    # 2. Commercieel/grond: alleen houden als er expliciet naar WONEN wordt
    #    verwezen (anders glipt 'herontwikkeling attractiepark' erdoor).
    if any(u in tekst for u in UITSLUITEN):
        return any(w in tekst for w in NAAR_WONEN)

    # 3. Gewoon een woning
    return any(w in tekst for w in WONING)


def is_usable(item: dict) -> bool:
    """Zonder prijs én zonder enige maat valt er niets te beoordelen.
    Perceel telt mee: bij een transformatiepand of woonboerderij is de kavel
    vaak de waardedrager, ook als het woonoppervlak niet vermeld staat."""
    prijs = item.get("price")
    opp = item.get("living_area")
    perceel = item.get("plot_area")
    return bool((prijs and prijs > 0) or (opp and opp > 0) or (perceel and perceel > 0))


def keep(item: dict) -> bool:
    """Een woning blijft altijd staan — ook zonder prijs of m² (bij veilingen
    is dat normaal; het blijft een lead). Alleen niet-woningen en objecten
    zonder enige aanwijzing vallen af."""
    if residential_only():
        return is_residential(item)
    return is_usable(item)


def filter_items(items: list[dict], bron: str = "") -> list[dict]:
    """Filtert een scrape-resultaat en logt wat er afvalt."""
    if not items:
        return items
    over = [i for i in items if keep(i)]
    weg = len(items) - len(over)
    if weg:
        print(f"[filter] {bron}: {weg} van {len(items)} weggefilterd "
              f"(geen woning of te weinig data)", flush=True)
    return over
