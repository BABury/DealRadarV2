"""Scenario-engine (Vastgoed Scan): flip / splitsen / verhuur per verbouwbudget.

Rekent elk object door tegen een aannamenprofiel. De verkoopwaarde per m² wordt
NIET hardcoded maar afgeleid van de stadsmediaan uit de eigen database:
  gerenoveerde eengezinswoning  = stadsmediaan × renov_uplift
  gerenoveerd appartement       = woningwaarde × app_premium
Zo schaalt de Top 5 automatisch mee per stad. Profielen zijn per week aan te
passen via het dashboard; elke aanname is te overrulen.

Alle uitkomsten zijn indicaties vóór financieringskosten en belasting.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics

from .split import analyse as split_analyse

REGIO_EINDHOVEN = [
    "eindhoven", "veldhoven", "nuenen", "waalre", "best", "geldrop",
    "mierlo", "geldrop-mierlo", "son en breugel", "helmond",
    "valkenswaard", "eersel", "oirschot",
]

# Grote steden (G40-achtig) — de focus voor liquiditeit en exit.
GROTE_STEDEN = [
    "amsterdam", "rotterdam", "den haag", "'s-gravenhage", "utrecht", "eindhoven",
    "groningen", "tilburg", "almere", "breda", "nijmegen", "enschede", "haarlem",
    "arnhem", "amersfoort", "zaanstad", "zaandam", "'s-hertogenbosch", "den bosch",
    "zwolle", "zoetermeer", "leiden", "maastricht", "dordrecht", "delft", "venlo",
    "deventer", "helmond", "hilversum", "alkmaar", "apeldoorn", "amstelveen",
]

# Randstad — dichtste markt, snelste doorverkoop.
RANDSTAD = [
    "amsterdam", "rotterdam", "den haag", "'s-gravenhage", "utrecht", "haarlem",
    "leiden", "delft", "zoetermeer", "amstelveen", "haarlemmermeer", "hoofddorp",
    "dordrecht", "almere", "zaandam", "zaanstad", "gouda", "alphen aan den rijn",
    "rijswijk", "capelle aan den ijssel", "schiedam", "vlaardingen",
]

REGIONS = {
    "grote_steden": GROTE_STEDEN,
    "randstad": RANDSTAD,
    "regio_eindhoven": REGIO_EINDHOVEN,
}

DEFAULT_PARAMS = {
    "ovb_pct": 8.0,          # overdrachtsbelasting beleggers/BV 2026
    "kk_vast": 6000.0,       # notaris/advies/overig
    "tiers": [1000, 1500, 2000, 2500],   # verbouwbudget €/m²
    "focus_tier": 1500,      # tier waarop gerangschikt wordt
    "renov_uplift": 1.12,    # gerenoveerd t.o.v. stadsmediaan €/m²
    "app_premium": 1.12,     # appartement-premie t.o.v. woning €/m²
    "waarde_band": 0.06,     # ± band om de waarde (laag/hoog)
    # Splitsen — de hoofdstrategie. Kosten schalen met het aantal appartementen.
    "split_kosten": 15000.0,     # vast: leges, splitsingsakte, VvE-oprichting, advies
    "split_per_unit": 20000.0,   # per EXTRA appartement: keuken, badkamer,
                                 # meterkast, brand- en geluidsscheiding
    "go_verlies_pct": 10.0,      # verkeersruimte (trappenhuis, entrees) bij splitsen
    "min_app_m2": 50,            # minimale appartementgrootte (check gemeente!)
    "huur_m2": 20.0,         # kale huur €/m²/maand
    "opex_pct": 25.0,        # exploitatiekosten verhuur
    # Financiering / looptijd — nodig voor een eerlijke (netto) winst
    "rente_pct": 5.5,        # financieringsrente %/jaar over de inleg
    "looptijd_mnd": 9,       # aankoop -> verkoop, incl. verbouw
    # Verkoopkosten (zoals in een echte development-P&L)
    "courtage_pct": 1.25,    # verkoopcourtage % van GDV
    "verkoop_vast": 2385.0,  # fotografie/brochure/notaris/royement e.d.
    "doel_roi_pct": 20.0,    # doelrendement -> bepaalt het maximale bod
    # Risico-drempels voor de Top (0 = uit) — een deal moet ook conservatief lonen
    "min_conservatief": 0,   # min. conservatieve nettowinst € (na financiering)
    "min_roi_pct": 0,        # min. conservatieve ROI % op de inleg
    "min_marge_pct": 0,      # min. veiligheidsmarge % (hoever verkoopprijs mag dalen)
    # Top 5-filters (0 = geen limiet) — per profiel instelbaar
    "min_area": 0,           # minimale woonoppervlakte m²
    "max_area": 0,           # maximale woonoppervlakte m²
    "min_price": 0,          # minimale vraagprijs €
    "max_price": 0,          # maximale vraagprijs €
    "max_price_m2": 0,       # maximale aankoopprijs €/m²
    "alleen_splitsbaar": 0,  # 1 = alleen objecten die in appartementen kunnen
    "min_units": 0,          # minimaal aantal appartementen na splitsing
    "max_units": 0,          # maximaal aantal (0 = geen limiet). Boven ~20 is het een
                             # ontwikkelproject: het splitsmodel rekent daar te optimistisch
}

# Vanaf hier is het geen 'splits-flip' meer maar een transformatieproject
# (vergunningstraject, parkeernorm, jaren doorlooptijd). Cijfers dan alleen indicatief.
ONTWIKKELPROJECT_UNITS = 20

# Vanaf zoveel appartementen rekenen we niet meer met 'verkopen als één woning'
FLIP_MAX_UNITS = 4

# Boven dit oppervlak hoort een object in het tabblad 'Grote projecten'
PROJECT_M2 = 1200

SEED_PROFILES = {
    "standaard":    {},
    "conservatief": {"renov_uplift": 1.05, "app_premium": 1.08,
                     "focus_tier": 2000, "waarde_band": 0.08},
    "agressief":    {"renov_uplift": 1.18, "app_premium": 1.15,
                     "focus_tier": 1200, "tiers": [1000, 1200, 1500, 2000]},
}


def merged_params(overrides: dict | None) -> dict:
    p = dict(DEFAULT_PARAMS)
    for k, v in (overrides or {}).items():
        if k in p and v is not None:
            p[k] = v
    return p


def city_medians_all() -> dict[str, float]:
    from .db import Listing, SessionLocal
    per_city: dict[str, list[float]] = {}
    with SessionLocal() as s:
        for l in s.query(Listing).filter(Listing.price_m2.isnot(None),
                                         Listing.price_m2 > 0,
                                         Listing.is_demo.is_(False)):
            if l.city:
                per_city.setdefault(l.city.lower(), []).append(l.price_m2)
    return {c: statistics.median(v) for c, v in per_city.items() if len(v) >= 3}


def city_counts_all() -> dict[str, int]:
    """Aantal referentie-objecten per stad — maat voor databetrouwbaarheid."""
    from .db import Listing, SessionLocal
    per_city: dict[str, int] = {}
    with SessionLocal() as s:
        for l in s.query(Listing).filter(Listing.price_m2.isnot(None),
                                         Listing.price_m2 > 0,
                                         Listing.is_demo.is_(False)):
            if l.city:
                per_city[l.city.lower()] = per_city.get(l.city.lower(), 0) + 1
    return per_city


def _confidence(n_comps: int, listing: dict) -> float:
    """0.4–1.0: schaalt met aantal comps en volledigheid van de objectdata."""
    base = min(1.0, 0.5 + n_comps / 40.0)
    missing = sum(1 for k in ("energy_label", "build_year") if not listing.get(k))
    return max(0.4, round(base * (1 - 0.12 * missing), 3))


def _motivated(listing: dict) -> tuple[float, list[str]]:
    """Boost voor gemotiveerde verkoper (prijsverlaging / veiling)."""
    factor, tags = 1.0, []
    hist = listing.get("price_history") or []
    drops = [h for h in hist if isinstance(h, dict) and h.get("to", 0) < h.get("from", 0)]
    if drops:
        factor += 0.12 * min(len(drops), 2)
        tags.append(f"{len(drops)}× prijsverlaging")
    if (listing.get("source") or "") in AUCTION_SOURCES_SC:
        if "[executieveiling]" in (listing.get("context") or "").lower():
            factor += 0.20
            tags.append("executieveiling")
        else:
            factor += 0.15
            tags.append("veiling")
    # Lang te koop = onderhandelruimte, vaak nog vóór de eerste prijsverlaging
    dom = listing.get("days_on_market")
    if dom is not None:
        if dom >= 180:
            factor += 0.18; tags.append(f"{dom}d te koop")
        elif dom >= 90:
            factor += 0.10; tags.append(f"{dom}d te koop")
    return min(factor, 1.5), tags


AUCTION_SOURCES_SC = {"vastgoedveiling", "veilingnotaris", "bog_auctions", "biedboek"}


def max_bid(gdv: float, verbouw: float, p: dict, doel_roi_pct: float | None = None) -> int:
    """Hoogste aankoopprijs waarbij het doelrendement nog gehaald wordt.

    Onmisbaar bij veilingen (daar is géén vraagprijs) en bij onderhandelen:
    het antwoord op 'tot hoever kan ik gaan?'.

    Zelfde ROI-definitie als de rest van DealRadar: winst / investering.

        investering  I = P*(1+ovb) + kk + verbouw
        financiering    = I * r          (r = rente × looptijd)
        winst           = GDV - I - I*r - verkoopkosten
        winst / I >= doel   =>   I <= (GDV - verkoopkosten) / (1 + r + doel)
    """
    doel = (doel_roi_pct if doel_roi_pct is not None else p.get("doel_roi_pct", 20)) / 100
    r = p.get("rente_pct", 0) / 100 * p.get("looptijd_mnd", 0) / 12
    vk = gdv * p.get("courtage_pct", 0) / 100 + p.get("verkoop_vast", 0)
    inv_max = (gdv - vk) / (1 + r + doel)
    ruimte = inv_max - p["kk_vast"] - verbouw
    return int(round(max(0.0, ruimte / (1 + p["ovb_pct"] / 100))))


PREMIE_MIN_N = 5          # min. aantal objecten per segment om marktdata te vertrouwen
PREMIE_PLAFOND = 1.40     # nooit meer dan 40% meer per m² aannemen


def split_premie(bm, listing: dict, area: float, unit_m2: float,
                 p: dict) -> tuple[float, str]:
    """Meerprijs per m² van de appartementen t.o.v. het hele huis.

    Dit ís de splitswinst: kleine appartementen verkopen voor meer per m² dan
    grote huizen. We halen die verhouding uit de marktdata per grootte-segment
    (bv. 'midden' 50-100 m² vs 'groot' >100 m²) in dezelfde stad/wijk. Te weinig
    data -> de vaste aanname uit het profiel.
    Het profiel schaalt de marktpremie mee (conservatief = voorzichtiger)."""
    vast = p["app_premium"]
    if bm is None:
        return vast, "aanname"
    args = (listing.get("city"), listing.get("neighbourhood"), listing.get("postcode"))
    huis, _ = bm.lookup(*args, area)
    app, label = bm.lookup(*args, unit_m2)
    if not (huis and app and huis.median and app.median) or huis is app:
        return vast, "aanname"
    if huis.n < PREMIE_MIN_N or app.n < PREMIE_MIN_N:
        return vast, f"aanname (markt te dun: n={huis.n}/{app.n})"
    markt = app.median / huis.median
    # profiel-schaal: standaard (1.12) = pure markt, conservatief (1.08) = 2/3
    schaal = (vast - 1) / 0.12 if vast > 1 else 1.0
    premie = 1 + (markt - 1) * schaal
    premie = max(1.0, min(PREMIE_PLAFOND, premie))
    return round(premie, 3), f"markt {app.median:,.0f}/{huis.median:,.0f} €/m² ({label})"


def scenario_table(listing: dict, params: dict, city_median: float | None,
                   bm=None) -> dict:
    """listing: dict met price, living_area, city (to_dict() van een Listing).

    Zonder prijs (veiling) rekenen we alsnog door op basis van het maximale bod."""
    price = listing.get("price") or 0
    area = listing.get("living_area") or 0
    if not area:
        return {"error": "woonoppervlak ontbreekt"}
    geen_prijs = not price
    if geen_prijs:
        price = 0.0
    if not city_median:
        return {"error": f"te weinig marktdata voor {listing.get('city')} (min. 3 objecten nodig)"}

    p = params
    huis_m2 = city_median * p["renov_uplift"]
    band = p["waarde_band"]
    ovb = price * p["ovb_pct"] / 100
    basis = price + ovb + p["kk_vast"]
    verkoopbaar = area * (1 - p["go_verlies_pct"] / 100)

    # Splitsanalyse: hoeveel appartementen, en hoe zeker (vergunning > genoemd >
    # potentieel). Kosten schalen met het aantal eenheden.
    sa = split_analyse(listing, p.get("min_app_m2", 50), p["go_verlies_pct"])
    split_allowed = sa["status"] != "nee"
    units = sa["units"]
    split_kosten = p["split_kosten"] + p.get("split_per_unit", 0) * max(0, units - 1)
    premie, premie_bron = (split_premie(bm, listing, area, sa["unit_m2"], p)
                           if split_allowed else (p["app_premium"], "n.v.t."))
    app_m2 = huis_m2 * premie

    rente = p.get("rente_pct", 5.5) / 100
    looptijd = p.get("looptijd_mnd", 9) / 12.0

    rows = []
    for tier in p["tiers"]:
        verbouw = tier * area
        inv_flip = basis + verbouw
        inv_split = inv_flip + split_kosten
        fin_flip = inv_flip * rente * looptijd      # financieringskosten (holding)
        fin_split = inv_split * rente * looptijd
        opbr_flip = area * huis_m2
        opbr_split = verkoopbaar * app_m2
        # verkoopkosten (courtage % van GDV + vaste kosten)
        vk_flip = opbr_flip * p.get("courtage_pct", 0) / 100 + p.get("verkoop_vast", 0)
        vk_split = opbr_split * p.get("courtage_pct", 0) / 100 + p.get("verkoop_vast", 0)
        # netto = opbrengst - investering - financiering - verkoopkosten
        flip_mid = opbr_flip - inv_flip - fin_flip - vk_flip
        split_mid = (opbr_split - inv_split - fin_split - vk_split) if split_allowed else None
        jaarhuur = verkoopbaar * p["huur_m2"] * 12
        row = {
            "tier": tier,
            "investering_flip": round(inv_flip),
            "investering_split": round(inv_split),
            "financiering_flip": round(fin_flip),
            "financiering_split": round(fin_split),
            "kostprijs_m2": round((inv_flip + fin_flip) / area),
            "flip_laag": round(opbr_flip * (1 - band) - inv_flip - fin_flip - vk_flip),
            "flip_mid": round(flip_mid),
            "flip_hoog": round(opbr_flip * (1 + band) - inv_flip - fin_flip - vk_flip),
            "split_allowed": split_allowed,
            "split_laag": round(opbr_split * (1 - band) - inv_split - fin_split - vk_split) if split_allowed else None,
            "split_mid": round(split_mid) if split_allowed else None,
            "split_hoog": round(opbr_split * (1 + band) - inv_split - fin_split - vk_split) if split_allowed else None,
            "bar_pct": round(jaarhuur / inv_split * 100, 1) if inv_split else None,
            "netto_pct": round(jaarhuur * (1 - p["opex_pct"] / 100) / inv_split * 100, 1)
                         if inv_split else None,
        }
        # Beste strategie op mid-scenario (splitsen alleen als toegestaan).
        # Vanaf FLIP_MAX_UNITS appartementen is 'flip' (als één woning verkopen)
        # geen realistische exit meer: niemand koopt een politiebureau van
        # 1.100 m² als eengezinswoning. Dan telt alleen splitsen.
        cand = []
        if not (split_allowed and units >= FLIP_MAX_UNITS):
            cand.append(("flip", flip_mid, inv_flip, opbr_flip, row["flip_laag"]))
        if split_allowed:
            cand.append(("split", split_mid, inv_split, opbr_split, row["split_laag"]))
        best = max(cand, key=lambda c: c[1])
        strat, best_mid, best_inv, best_opbr, best_laag = best
        # Veiligheidsmarge: hoever mag de verkoopprijs zakken vóór break-even
        # (inclusief financiering) t.o.v. het mid-scenario.
        be_opbr = best_inv + (best_inv * rente * looptijd)
        mos = (best_opbr - be_opbr) / best_opbr * 100 if best_opbr else 0
        row.update({
            "max_bod_flip": max_bid(opbr_flip, verbouw, p),
            "max_bod_split": (max_bid(opbr_split, verbouw + split_kosten, p)
                              if split_allowed else None),
            "best_mid": round(best_mid),
            "best_laag": round(best_laag),          # conservatieve nettowinst
            "best_strategie": "splitsen" if strat == "split" else "flip",
            "best_investering": round(best_inv),
            "roi_pct": round(best_mid / best_inv * 100, 1) if best_inv else None,
            "roi_laag_pct": round(best_laag / best_inv * 100, 1) if best_inv else None,
            "marge_pct": round(mos, 1),
        })
        rows.append(row)

    # Zonder vraagprijs (veiling) is elke "winst" fictief — die velden leggen
    # we leeg. Wat wél klopt en bruikbaar is: het maximale bod.
    if geen_prijs:
        for r in rows:
            for k in ("flip_laag", "flip_mid", "flip_hoog", "split_laag", "split_mid",
                      "split_hoog", "best_mid", "best_laag", "roi_pct", "roi_laag_pct",
                      "marge_pct", "bar_pct", "netto_pct"):
                r[k] = None
            r["best_strategie"] = "bied max"

    focus = next((r for r in rows if r["tier"] == p["focus_tier"]), rows[0])
    be_flip = (area * huis_m2 - basis) / area
    be_split = (verkoopbaar * app_m2 - basis - split_kosten) / area
    return {
        "aannames": {**p, "stadsmediaan_m2": round(city_median),
                     "huis_m2": round(huis_m2), "app_m2": round(app_m2),
                     "split_premie": premie, "split_premie_bron": premie_bron},
        "aankoop": {"prijs": price, "ovb": round(ovb), "all_in": round(basis),
                    "prijs_m2": round(price / area)},
        "split_allowed": split_allowed,
        "split": {**sa, "kosten": round(split_kosten), "premie": premie,
                  "premie_bron": premie_bron},
        "geen_prijs": geen_prijs,
        "breakeven_flip_m2": round(be_flip),
        "breakeven_split_m2": round(be_split),
        "rows": rows,
        "focus": focus,
    }


def top_listings(profile_params: dict, cities: list[str] | None = None,
                 n: int = 5, min_score: int = 0, rank: str = "risk",
                 region: str = "grote_steden", soort: str = "alles") -> dict:
    """Rangschik objecten. rank:
    'risk'  = deal_score = conservatieve nettowinst × betrouwbaarheid × motivatie (default)
    'roi'   = conservatieve ROI % op de inleg (kapitaal-efficiënt, min. moeite)
    'winst' = ruwe mid-upside (oud gedrag)
    'nieuw' = nieuwste vondsten eerst · 'oud' = oudste eerst.
    region: 'focus' (de focussteden) | 'grote_steden' | 'randstad' | 'regio_eindhoven' | 'alle',
    of geef `cities` expliciet mee (overrulet de regio)."""
    from .db import Listing, SessionLocal
    if cities:
        city_set = {c.lower() for c in cities}
        all_cities = False
    elif region == "alle":
        city_set, all_cities = set(), True
    elif region == "focus":
        # De focussteden uit het dashboard: waar splitsen in meerdere woningen zin heeft
        from .agents.instellingen import focus_varianten
        city_set, all_cities = focus_varianten(), False
    else:
        city_set = {c.lower() for c in REGIONS.get(region, GROTE_STEDEN)}
        all_cities = False
    medians = city_medians_all()
    counts = city_counts_all()
    from .benchmarks import BenchmarkMap
    bm = BenchmarkMap()      # één keer laden; nodig voor de splits-premie
    params = merged_params(profile_params)

    results = []
    with SessionLocal() as s:
        q = (s.query(Listing)
             .filter(Listing.is_demo.is_(False),
                     Listing.living_area.isnot(None), Listing.living_area > 0))
        if min_score:
            q = q.filter(Listing.flip_score >= min_score)
        for l in q.all():
            # Veiling zonder vraagprijs: waarderen op het MAXIMALE BOD — "win je
            # 'm voor ≤ dit bedrag, dan haal je je doelrendement". Dat is bij een
            # veiling het getal waar het om draait.
            veiling = not (l.price and l.price > 0)
            if veiling and (l.source or "") not in AUCTION_SOURCES_SC:
                continue
            # 'koop' = echte vraagprijs, 'veiling' = gewaardeerd op max. bod.
            # Niet door elkaar ranken: een veiling op max. bod scoort per definitie
            # het doelrendement en verdringt anders alle koopwoningen.
            if soort == "koop" and veiling:
                continue
            if soort == "veiling" and not veiling:
                continue
            if not all_cities and (l.city or "").lower() not in city_set:
                continue
            # Huurder blijft na de veiling: niet leeg te verbouwen/splitsen -> nooit een kans
            if "huurbeding ingeroepen" in (l.context or "").lower():
                continue
            # profielfilters (0 = uit)
            if params["min_area"] and l.living_area < params["min_area"]:
                continue
            if params["max_area"] and l.living_area > params["max_area"] and soort != "project":
                continue
            d = l.to_dict()
            med = medians.get((l.city or "").lower())
            max_bod = None
            if veiling:
                t0 = scenario_table(d, params, med, bm)
                if "error" in t0:
                    continue          # geen verkoopprijzen voor deze stad
                f0 = t0["focus"]
                max_bod = (f0.get("max_bod_split") if t0["split_allowed"] and f0.get("max_bod_split")
                           else f0.get("max_bod_flip"))
                if not max_bod:
                    continue
                d = {**d, "price": float(max_bod), "price_m2": round(max_bod / l.living_area)}
            if params["min_price"] and d["price"] < params["min_price"]:
                continue
            if params["max_price"] and d["price"] > params["max_price"]:
                continue
            if params["max_price_m2"] and (d.get("price_m2") or 0) > params["max_price_m2"]:
                continue
            tab = scenario_table(d, params, med, bm)
            if "error" in tab:
                continue
            f = tab["focus"]
            sa = tab["split"]

            # Tabblad-indeling. Grote projecten apart: biedboek (overheids-
            # inschrijvingen) en alles boven ~20 appartementen of 1.200 m² —
            # daar rekent het splitsmodel alleen indicatief.
            is_project = ((l.source or "") == "biedboek"
                          or sa["units"] > ONTWIKKELPROJECT_UNITS
                          or (l.living_area or 0) > PROJECT_M2)
            categorie = "project" if is_project else ("veiling" if veiling else "koop")
            if soort != "alles" and categorie != soort:
                continue

            # Splitsen is de hoofdstrategie: optioneel alleen splitsbare objecten
            if params.get("alleen_splitsbaar") and not tab["split_allowed"]:
                continue
            if params.get("min_units") and sa["units"] < params["min_units"]:
                continue
            if params.get("max_units") and sa["units"] > params["max_units"] and soort != "project":
                continue

            conf = _confidence(counts.get((l.city or "").lower(), 0), d)
            motiv, motiv_tags = _motivated(d)
            best_laag = f.get("best_laag", f["best_mid"])
            deal_score = max(0, best_laag) * conf * motiv
            # Splitswinst weegt mee naar zekerheid: een vergunning is geld waard,
            # 'potentieel' betekent dat de gemeente nog akkoord moet geven.
            if f["best_strategie"] == "splitsen":
                deal_score *= sa["zekerheid"]
            deal_score = round(deal_score)

            # Risico-drempels (0 = uit)
            if params["min_conservatief"] and best_laag < params["min_conservatief"]:
                continue
            if params["min_roi_pct"] and (f.get("roi_laag_pct") or -999) < params["min_roi_pct"]:
                continue
            if params["min_marge_pct"] and (f.get("marge_pct") or -999) < params["min_marge_pct"]:
                continue
            # Alleen deals die óók conservatief lonen. Geldt ook bij sorteren op
            # datum: 'nieuwste eerst' moet nieuwe KANSEN tonen, geen verliesposten.
            if rank in ("risk", "roi", "nieuw", "oud") and best_laag <= 0:
                continue

            results.append({
                "id": l.id, "address": l.address, "city": l.city,
                "neighbourhood": l.neighbourhood, "url": l.url,
                "photo_url": l.photo_url,
                "price": None if veiling else l.price, "living_area": l.living_area,
                "price_m2": None if veiling else l.price_m2, "energy_label": l.energy_label,
                "veiling": veiling, "max_bod": max_bod, "auction_date": l.auction_date,
                "source": l.source,
                "build_year": l.build_year, "flip_score": l.flip_score,
                "score_breakdown": l.score_breakdown,
                "best_mid": f["best_mid"], "best_laag": best_laag,
                "best_strategie": f["best_strategie"],
                "split_allowed": tab.get("split_allowed", True),
                "units": sa["units"], "split_status": sa["status"],
                "ontwikkelproject": sa["units"] > ONTWIKKELPROJECT_UNITS,
                "categorie": categorie,
                "split_reden": sa["reden"], "unit_m2": sa["unit_m2"],
                "split_kosten": sa["kosten"], "split_premie": sa["premie"],
                "split_premie_bron": sa["premie_bron"],
                "flip_mid": f["flip_mid"], "split_mid": f.get("split_mid"),
                "roi_pct": f.get("roi_pct"), "roi_laag_pct": f.get("roi_laag_pct"),
                "marge_pct": f.get("marge_pct"), "financiering": f.get("financiering_flip"),
                "bar_pct": f["bar_pct"], "kostprijs_m2": f["kostprijs_m2"],
                "confidence": conf, "motivated": round(motiv, 2),
                "motivated_tags": motiv_tags, "deal_score": deal_score,
                # wanneer deze kans voor het eerst is gevonden
                "first_seen": l.first_seen.isoformat() if l.first_seen else None,
                "days_on_market": d.get("days_on_market"),
                "dagen_bekend": ((dt.datetime.utcnow() - l.first_seen).days
                                 if l.first_seen else None),
                "breakeven_flip_m2": tab["breakeven_flip_m2"],
                "breakeven_split_m2": tab["breakeven_split_m2"],
            })

    key = {"risk": lambda r: r["deal_score"],
           "roi": lambda r: (r.get("roi_laag_pct") or -999),
           "winst": lambda r: r["best_mid"],
           # nieuwste eerst / oudste eerst op moment van vinden
           "nieuw": lambda r: (r.get("first_seen") or ""),
           "oud": lambda r: (r.get("first_seen") or "")}.get(
        rank, lambda r: r["deal_score"])
    results.sort(key=key, reverse=(rank != "oud"))

    # Dubbelingen eruit: veilingsites plaatsen elkaars veilingen door, dus
    # hetzelfde object komt onder meerdere URL's binnen. Zelfde stad + zelfde
    # oppervlak + zelfde prijs/bod = hetzelfde object; de hoogst gerankte blijft.
    uniek, gezien = [], set()
    for r in results:
        sleutel = ((r.get("city") or "").strip().lower(), round(r.get("living_area") or 0),
                   round((r.get("price") or r.get("max_bod") or 0) / 1000))
        if sleutel in gezien:
            continue
        gezien.add(sleutel)
        uniek.append(r)
    results = uniek
    return {
        "generated": dt.datetime.utcnow().isoformat(),
        "params": params,
        "rank": rank,
        "soort": soort,
        "region": region if not cities else "custom",
        "cities": sorted(city_set) if not all_cities else "alle",
        "beoordeeld": len(results),
        "top": results[:n],
    }


# ── profielen in de database ─────────────────────────────────────

def list_profiles() -> list[dict]:
    from .db import Profile, SessionLocal
    with SessionLocal() as s:
        return [{"name": p.name, "params": merged_params(json.loads(p.params or "{}")),
                 "updated": p.updated.isoformat() if p.updated else None}
                for p in s.query(Profile).order_by(Profile.name).all()]


def get_profile(name: str) -> dict:
    from .db import Profile, SessionLocal
    with SessionLocal() as s:
        p = s.query(Profile).filter_by(name=(name or "standaard")).one_or_none()
        return merged_params(json.loads(p.params) if p else {})


def save_profile(name: str, params: dict) -> dict:
    from .db import Profile, SessionLocal
    clean = {k: v for k, v in (params or {}).items() if k in DEFAULT_PARAMS}
    with SessionLocal() as s:
        p = s.query(Profile).filter_by(name=name).one_or_none()
        if p:
            p.params = json.dumps(clean)
            p.updated = dt.datetime.utcnow()
        else:
            s.add(Profile(name=name, params=json.dumps(clean),
                          updated=dt.datetime.utcnow()))
        s.commit()
    return {"ok": True, "name": name, "params": merged_params(clean)}


def seed_profiles() -> None:
    from .db import Profile, SessionLocal
    with SessionLocal() as s:
        existing = {p.name for p in s.query(Profile).all()}
        for name, overrides in SEED_PROFILES.items():
            if name not in existing:
                s.add(Profile(name=name, params=json.dumps(overrides),
                              updated=dt.datetime.utcnow()))
        s.commit()
