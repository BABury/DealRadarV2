"""Vliering-scenario: onbenutte inpandige ruimte omzetten in een extra woning.

Dit staat los van het splitsscenario in `scenarios.py`, en dat is bewust.
Splitsen *verdeelt* bestaand woonoppervlak over twee woningen — de winst komt
uit het feit dat twee kleine woningen samen meer opbrengen dan één grote.
De vliering *voegt* oppervlak toe: de bergzolder telt vandaag voor niets mee
in de vraagprijs, en wordt morgen een appartement dat je apart verkoopt.

Rekenen blijft code, precies zoals bij de rest van DealRadar: de agents leveren
alleen feiten en bezwaren, nooit een bedrag.

Alle uitkomsten zijn indicaties vóór financiering en belasting.
"""
from __future__ import annotations

# Een zolderappartement brengt minder op dan de mediaan van de stad: lagere
# plafonds, dakramen in plaats van gevelramen, vaak geen buitenruimte en een
# trap extra. Liever te voorzichtig rekenen dan jezelf rijk.
KORTING_ZOLDERWONING = 0.85

# Onder deze maat is het geen zelfstandige woning maar een hobbykamer.
ABSOLUUT_MINIMUM_M2 = 15.0


def _instellingen() -> dict:
    from .agents.instellingen import lees
    return lees()


def bruikbare_ruimte(listing: dict) -> float:
    """Hoeveel onbenutte inpandige m² heeft dit object?

    Funda zet de vliering onder 'overige inpandige ruimte' omdat de kap de
    meetnorm voor woonoppervlak niet haalt. Externe bergruimte (een schuur in
    de tuin) telt hier niet mee: daar maak je geen appartement van.
    """
    return float(listing.get("overige_inpandig") or 0)


def verborgen_hoogte(listing: dict) -> float | None:
    """Inhoud gedeeld door woonoppervlak.

    Een woning van 100 m² met 420 m³ inhoud heeft ergens hoogte die nergens
    als woonruimte meetelt. Dat is het tweede spoor, voor de panden waar de
    makelaar de zolder niet apart heeft opgegeven.
    """
    inhoud = listing.get("inhoud_m3")
    wonen = listing.get("living_area")
    if not inhoud or not wonen or wonen <= 0:
        return None
    return round(float(inhoud) / float(wonen), 2)


def kansrijk(listing: dict, drempel_m2: float | None = None) -> bool:
    """Is dit een object om naar te kijken voor de vliering-strategie?"""
    if drempel_m2 is None:
        drempel_m2 = _instellingen()["vliering_min_m2"]
    if bruikbare_ruimte(listing) >= max(drempel_m2, ABSOLUUT_MINIMUM_M2):
        return True
    vh = verborgen_hoogte(listing)
    if vh is not None and vh >= 4.2:
        return True
    return bool((listing.get("flags") or {}).get("vliering"))


def bereken(listing: dict, mediaan_m2: float | None,
            params: dict | None = None) -> dict | None:
    """Wat levert die onbenutte ruimte op als je er een appartement van maakt?

    Geeft None als er niets te rekenen valt (geen ruimte, of geen benchmark
    voor die stad). Liever niets tonen dan een verzonnen bedrag.
    """
    p = params or _instellingen()
    extra = bruikbare_ruimte(listing)
    reden = []

    if extra < max(p["vliering_min_m2"], ABSOLUUT_MINIMUM_M2):
        # Geen opgegeven ruimte: dan alleen schatten als de inhoud verraadt
        # dat er hoogte over is, en dan voorzichtig.
        vh = verborgen_hoogte(listing)
        wonen = float(listing.get("living_area") or 0)
        if vh is None or vh < 4.2 or wonen <= 0:
            return None
        # Ruwe schatting: de inhoud boven 3,0 m³ per m² woonoppervlak zit in
        # de kap. Gedeeld door 2,6 m hoogte geeft een vloeroppervlak, en
        # daarvan is in de praktijk maar een deel bruikbaar (schuine kap).
        extra = round(max(0.0, (vh - 3.0)) * wonen / 2.6 * 0.6)
        if extra < ABSOLUUT_MINIMUM_M2:
            return None
        reden.append(f"geschat uit inhoud ({vh} m³/m²), niet door de makelaar opgegeven")
    else:
        reden.append(f"{extra:.0f} m² overige inpandige ruimte op de Funda-pagina")

    if not mediaan_m2 or mediaan_m2 <= 0:
        return None

    waarde_m2 = mediaan_m2 * KORTING_ZOLDERWONING
    bruto = extra * waarde_m2
    verbouw = extra * p["vliering_verbouw_eur_m2"]
    vast = p["vliering_vaste_kosten"]
    netto = bruto - verbouw - vast

    if (listing.get("flags") or {}).get("vliering"):
        reden.append("omschrijving noemt een vliering of bergzolder")
    if listing.get("erfpacht"):
        reden.append("let op: erfpacht — canon kan bij extra meters herzien worden")

    return {
        "extra_m2": round(extra),
        "waarde_per_m2": round(waarde_m2),
        "bruto": round(bruto),
        "verbouw": round(verbouw),
        "vaste_kosten": round(vast),
        "netto": round(netto),
        "reden": reden,
        "aannames": (f"verkoopwaarde = {int(KORTING_ZOLDERWONING * 100)}% van de "
                     f"stadsmediaan, verbouw €{p['vliering_verbouw_eur_m2']:.0f}/m², "
                     f"vaste kosten €{p['vliering_vaste_kosten']:.0f}. "
                     "Vóór financiering en belasting."),
    }
