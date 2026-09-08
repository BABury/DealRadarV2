"""Tekstanalyse van omschrijvingen — geport uit recap.py."""
from __future__ import annotations

import re

KEYWORD_FLAGS = {
    "flag_casco": ["casco oplevering", "casco"],
    # Ontwikkel- én transformatiesignalen. De transformatietermen zijn in de
    # praktijk bewezen op funda in business (kerken/scholen/kantoren die naar
    # wonen gaan) — daar zit de grootste waardesprong.
    "flag_ontwikkeling": ["ontwikkelingsmogelijkheden", "ontwikkeling mogelijk", "ontwikkelpotentie",
                          "vergunning verleend", "vergunning aanwezig", "omgevingsvergunning",
                          "bouwvergunning", "bestemmingsplan biedt",
                          "herontwikkeling", "herontwikkelen", "herontwikkelingskans",
                          "herontwikkelingsmogelijkheid", "herbestemming", "herbestemmen",
                          "transformatie", "transformeren", "transformatiepand",
                          "transformatieobject", "transformatielocatie",
                          "naar woningen", "tot woningen", "naar appartementen",
                          "tot appartementen", "naar wonen", "woningbouw mogelijk",
                          "geschikt voor woningbouw", "woonbestemming",
                          "bestemmingswijziging", "voormalige kerk", "voormalig kerkgebouw",
                          "voormalig kerkelijk", "voormalige school", "voormalig schoolgebouw"],
    "flag_splits_bouwkundig": ["bouwkundige splitsing", "bouwkundig splitsen"],
    "flag_splits_kadastraal": ["kadastrale splitsing", "kadastraal splitsen"],
    "flag_splitsvergunning": ["splitsingsvergunning"],
    "flag_dakopbouw": ["dakopbouw mogelijk", "dakopbouw", "optoppen", "optopping",
                       "opbouw mogelijk", "extra verdieping", "dakterras mogelijk"],
    "flag_uitbreiden": ["aanbouw mogelijk", "uitbreiding mogelijk", "uitbreiden", "aanbouw",
                        "uitbouw mogelijk", "uit te bouwen", "uitbouwen", "uitbouw",
                        "bijgebouw", "bijgebouwen", "bouwkavel", "royaal perceel",
                        "groot perceel", "riant perceel", "bouwmogelijkheden",
                        "vergunningsvrij", "vergunningvrij"],
    "flag_verbouw": ["verbouwmogelijkheden", "verbouwing", "te renoveren", "opknappen",
                     "opknappertje", "renovatie", "te moderniseren", "achterstallig onderhoud",
                     "handige klusser", "klushuis", "kluswoning", "eigen smaak",
                     "naar eigen inzicht", "moderniseren"],
    "flag_verhuurd": ["verhuurmogelijkheden", "verhuurd", "belegging", "huurinkomsten", "huurder"],
}

_MAINTENANCE = [
    (["slechte staat", "slecht onderhoud", "achterstallig onderhoud", "casco"], "Slecht"),
    (["matige staat", "matig onderhoud", "enig achterstallig", "opknapper"], "Matig"),
    (["goede staat", "goed onderhouden", "goed onderhoud"], "Goed"),
    (["uitstekende staat", "perfect onderhouden", "instapklaar", "instapklare"], "Uitstekend"),
]


def _context(text: str, keyword: str, max_chars: int = 180) -> str:
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, text.rfind(".", 0, idx) + 1)
    end_dot = text.find(".", idx)
    end = end_dot + 1 if end_dot != -1 else min(len(text), idx + max_chars)
    return text[start:end].strip()[:max_chars]


def analyse_description(text: str) -> dict:
    """Geeft flag_*-velden, onderhoudsstaat, erfpacht en context-snippets terug."""
    out: dict = {}
    lower = (text or "").lower()

    snippets = []
    for flag, patterns in KEYWORD_FLAGS.items():
        hit = next((p for p in patterns if p in lower), "")
        out[flag] = bool(hit)
        if hit:
            snip = _context(text, hit)
            if snip:
                snippets.append(snip)
    out["context"] = " | ".join(dict.fromkeys(snippets))[:600]

    out["maintenance"] = ""
    for kws, label in _MAINTENANCE:
        if any(k in lower for k in kws):
            out["maintenance"] = label
            break

    out["erfpacht"] = "erfpacht" in lower
    return out


def parse_price(raw: str) -> float | None:
    """'€ 425.000 k.k.' → 425000.0"""
    if not raw:
        return None
    m = re.search(r"(\d[\d.\s]*\d|\d)", str(raw).replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(" ", ""))
    except ValueError:
        return None
