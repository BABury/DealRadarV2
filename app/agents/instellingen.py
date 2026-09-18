"""Instellingen van het agent-team — in het dashboard bij te stellen.

Het team werkt ALLEEN waar splitsen zin heeft: de focussteden. Buiten die
steden leest Lotte niets, zoekt Rik niets op en bekritiseert Kees niets.
Dat is waar het geld heen gaat, dus daar hoort de controle te liggen.

Opslag in de database (tabel `instellingen`), met deze standaarden als
vangnet. Elke waarde wordt begrensd, zodat een typefout in het dashboard
niet ineens honderden dollars kost.
"""
from __future__ import annotations

import os

SLEUTEL = "agents"

# Grote steden met vraag naar kleinere woningen (studenten, starters,
# eenpersoonshuishoudens) — dezelfde steden die de Funda-scraper afloopt.
FOCUS_STANDAARD = ["amsterdam", "rotterdam", "den haag", "utrecht", "eindhoven",
                   "haarlem", "leiden", "delft", "groningen", "nijmegen",
                   "arnhem", "zwolle"]

# Schrijfwijzen die in de bronnen voor dezelfde stad voorkomen.
ALIASSEN = {"den-haag": "den haag", "'s-gravenhage": "den haag", "s-gravenhage": "den haag",
            "'s-hertogenbosch": "den bosch", "s-hertogenbosch": "den bosch"}

STANDAARD = {
    "steden": FOCUS_STANDAARD,
    "dagbudget_usd": float(os.getenv("AGENT_MAX_USD_PER_DAY", "2.0")),
    "denkniveau": "low",            # low | medium | high — hoe diep Sonnet nadenkt
    # Het team werkt alleen op jouw opdracht (dashboard of Telegram). Zet dit
    # aan als je toch wilt dat het om 7:30 en 18:30 zelf een ronde draait.
    "automatische_rondes": False,
    # Lotte (Lezer)
    "lotte_aan": True,
    "lotte_per_ronde": 40,
    "lotte_min_m2": 110,            # zelfde ondergrens als profiel 'bob'
    "lotte_max_m2": 1200,           # boven 1.200 m² = project, niet jouw strategie
    "lotte_alleen_splitspotentie": True,
    # Rik (Regelchecker)
    "rik_aan": True,
    "rik_steden_per_ronde": 2,
    "rik_zoekopdrachten": 5,        # per stad; meer = grondiger én duurder
    "rik_geldig_dagen": 60,
    # Kees (Criticus)
    "kees_aan": True,
    "kees_top_n": 3,                # per soort (koop en veiling)
}

# (min, max) per getal: de rem op typefouten
GRENZEN = {
    "dagbudget_usd": (0.0, 25.0),
    "lotte_per_ronde": (0, 300),
    "lotte_min_m2": (0, 5000),
    "lotte_max_m2": (0, 20000),
    "rik_steden_per_ronde": (0, 10),
    "rik_zoekopdrachten": (1, 15),
    "rik_geldig_dagen": (7, 365),
    "kees_top_n": (0, 15),
}

# Gemeten in de eerste echte ronde (18 sept 2026); alleen voor de schatting.
KOSTEN_SCHATTING = {"lotte": 0.0045, "rik_per_zoekopdracht": 0.025, "kees": 0.014}


def stad(naam: str | None) -> str:
    """Eén schrijfwijze per stad."""
    n = (naam or "").strip().lower()
    return ALIASSEN.get(n) or n.replace("-", " ")


def lees() -> dict:
    uit = dict(STANDAARD)
    try:
        from ..db import instelling_lezen
        uit.update({k: v for k, v in instelling_lezen(SLEUTEL).items() if k in STANDAARD})
    except Exception as e:
        print(f"[agents] instellingen niet geladen, standaard gebruikt: {e}", flush=True)
    return _schoon(uit)


def _schoon(d: dict) -> dict:
    uit = dict(STANDAARD)
    for k, v in d.items():
        if k not in STANDAARD:
            continue
        std = STANDAARD[k]
        try:
            if isinstance(std, bool):
                uit[k] = v in (True, 1, "1", "true", "True", "aan", "on")
            elif isinstance(std, (int, float)):
                v = type(std)(float(v))
                lo, hi = GRENZEN.get(k, (None, None))
                uit[k] = max(lo, min(hi, v)) if lo is not None else v
            elif k == "steden":
                lijst = v.split(",") if isinstance(v, str) else list(v or [])
                uit[k] = sorted({stad(x) for x in lijst if str(x).strip()})
            elif k == "denkniveau":
                uit[k] = v if v in ("low", "medium", "high") else std
            else:
                uit[k] = v
        except (TypeError, ValueError):
            uit[k] = std
    return uit


def opslaan(nieuw: dict) -> dict:
    from ..db import instelling_opslaan
    d = _schoon({**lees(), **(nieuw or {})})
    instelling_opslaan(SLEUTEL, d)
    return d


def in_focus(naam: str | None) -> bool:
    return stad(naam) in set(lees()["steden"])


def focus_varianten(steden: list[str] | None = None) -> set[str]:
    """Alle schrijfwijzen van de focussteden zoals ze in de bronnen staan
    ('den haag', 'den-haag', "'s-gravenhage"), voor filters in de database."""
    uit: set[str] = set()
    for c in (steden if steden is not None else lees()["steden"]):
        c = stad(c)
        uit |= {c, c.replace(" ", "-")}
        uit |= {k for k, v in ALIASSEN.items() if v == c}
    return uit


def schatting(d: dict | None = None) -> dict:
    """Maximale kosten van één ronde als alles volloopt."""
    d = d or lees()
    lotte = (d["lotte_per_ronde"] if d["lotte_aan"] else 0) * KOSTEN_SCHATTING["lotte"]
    rik = ((d["rik_steden_per_ronde"] * d["rik_zoekopdrachten"] * KOSTEN_SCHATTING["rik_per_zoekopdracht"])
           if d["rik_aan"] else 0)
    kees = (2 * d["kees_top_n"] if d["kees_aan"] else 0) * KOSTEN_SCHATTING["kees"]
    return {"lotte": round(lotte, 3), "rik": round(rik, 3), "kees": round(kees, 3),
            "totaal_max": round(lotte + rik + kees, 3),
            "opmerking": "Rik kost alleen iets bij een nieuwe of verlopen stad; "
                         "daarna hergebruikt hij zijn uitkomst."}
