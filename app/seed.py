"""Voorbeelddata zodat het dashboard direct wat toont (gemarkeerd als 'demo').

Wordt automatisch geladen bij eerste start als de database leeg is
(SEED_ON_START=1, default). Verwijderen: POST /api/purge-demo.
Realistische maar fictieve objecten — in de UI zichtbaar met een demo-badge.
"""
from __future__ import annotations

import json
import random

from .db import Listing, SessionLocal
from .scoring import compute_scores

random.seed(42)

_ROWS = [
    # (stad, straat, prijs, m2, bouwjaar, label, onderhoud, flags, source, drop)
    ("Eindhoven", "Hastelweg 112", 385000, 128, 1935, "F", "Matig", ["verbouw", "dakopbouw"], "funda", 1),
    ("Eindhoven", "Leenderweg 245", 425000, 142, 1928, "G", "Slecht", ["verbouw", "splits_bouwkundig"], "funda", 0),
    ("Eindhoven", "Tongelresestraat 301", 349000, 110, 1930, "E", "Matig", ["verbouw"], "funda", 2),
    ("Eindhoven", "Strijpsestraat 88", 465000, 155, 1925, "F", "", ["casco", "ontwikkeling"], "funda", 0),
    ("Eindhoven", "Aalsterweg 190", 549000, 160, 1938, "D", "Goed", [], "funda", 0),
    ("Eindhoven", "Geldropseweg 77", 398000, 120, 1931, "E", "Matig", ["uitbreiden"], "funda", 1),
    ("Nuenen", "Berg 24", 595000, 178, 1955, "F", "Matig", ["verbouw", "uitbreiden"], "funda", 0),
    ("Veldhoven", "Kapelstraat-Zuid 62", 445000, 140, 1962, "E", "", ["verbouw"], "funda", 0),
    ("Arnhem", "Klarendalseweg 193", 285000, 105, 1910, "G", "Slecht", ["verbouw", "splits_bouwkundig"], "funda", 1),
    ("Arnhem", "Steenstraat 41", 325000, 118, 1908, "F", "Matig", ["verbouw", "splitsvergunning"], "funda", 0),
    ("Arnhem", "Cattepoelseweg 210", 550000, 165, 1932, "D", "", ["dakopbouw"], "funda", 0),
    ("Arnhem", "Hommelseweg 88", 298000, 98, 1915, "E", "Matig", ["verbouw"], "funda", 2),
    ("Nijmegen", "Willemsweg 154", 315000, 108, 1920, "F", "Matig", ["verbouw"], "funda", 0),
    ("Nijmegen", "Daalseweg 267", 365000, 125, 1912, "E", "", ["splits_kadastraal", "verbouw"], "funda", 1),
    ("Nijmegen", "Groesbeekseweg 89", 495000, 150, 1925, "D", "Goed", [], "funda", 0),
    ("Utrecht", "Amsterdamsestraatweg 412", 425000, 102, 1918, "F", "Matig", ["verbouw", "splits_bouwkundig"], "funda", 0),
    ("Utrecht", "Vleutenseweg 233", 465000, 112, 1922, "E", "", ["verbouw"], "funda", 1),
    ("Utrecht", "Croeselaan 187", 545000, 121, 1928, "D", "", ["dakopbouw"], "funda", 0),
    ("Haarlem", "Amsterdamstraat 56", 435000, 98, 1905, "F", "Matig", ["verbouw"], "funda", 0),
    ("Haarlem", "Zijlweg 148", 585000, 132, 1915, "E", "", ["splitsvergunning"], "funda", 0),
    ("Leiden", "Herenstraat 77", 425000, 105, 1910, "F", "", ["verbouw", "splits_bouwkundig"], "funda", 1),
    ("Den Haag", "Weimarstraat 264", 385000, 115, 1906, "G", "Slecht", ["verbouw", "splits_bouwkundig"], "funda", 0),
    ("Den Haag", "Paul Krugerlaan 133", 295000, 102, 1912, "F", "Matig", ["verbouw", "verhuurd"], "funda", 2),
    ("Den Haag", "Loosduinsekade 405", 345000, 122, 1920, "E", "", ["splits_kadastraal"], "funda", 0),
    # veilingen
    ("Eindhoven", "Woenselse Markt 14 (executieveiling)", 295000, 135, 1928, "G", "Slecht", ["verbouw", "casco"], "veilingnotaris", 0),
    ("Arnhem", "Johan de Wittlaan 62 (executieveiling)", 245000, 112, 1955, "F", "Slecht", ["verbouw"], "veilingnotaris", 0),
    ("Tilburg", "Korvelseweg 189 (veiling)", 265000, 128, 1925, "F", "Matig", ["verbouw", "splits_bouwkundig"], "veilingnotaris", 0),
    ("Utrecht", "Kanaalstraat 95 (online veiling)", 385000, 108, 1915, "E", "Matig", ["verbouw"], "bog_auctions", 0),
    ("Breda", "Haagweg 334 (inschrijving RVB)", 425000, 240, 1960, "G", "Slecht", ["ontwikkeling", "casco"], "biedboek", 0),
]


def seed() -> int:
    with SessionLocal() as s:
        if s.query(Listing).count() > 0:
            return 0
        for i, (city, street, price, m2, year, label, onderhoud, flags, source, drops) in enumerate(_ROWS):
            hist = []
            p = price
            for d in range(drops):
                old = round(p * (1 + 0.04 * (drops - d)))
                hist.append({"date": f"2026-0{4 + d}-15", "from": old, "to": p})
            slug = street.lower().split("(")[0].strip().replace(" ", "-")
            l = Listing(
                source=source,
                url=f"https://demo.flipradar.nl/{i}-{slug}",
                address=street,
                city=city,
                price=float(price),
                living_area=float(m2),
                price_m2=round(price / m2),
                property_type="woonhuis",
                build_year=year,
                rooms=random.randint(4, 7),
                energy_label=label,
                maintenance=onderhoud,
                erfpacht=random.random() < 0.15,
                auction_date="2026-07-21" if source != "funda" else "",
                is_demo=True,
                price_history=json.dumps(hist),
                flag_casco="casco" in flags,
                flag_verbouw="verbouw" in flags,
                flag_ontwikkeling="ontwikkeling" in flags,
                flag_dakopbouw="dakopbouw" in flags,
                flag_uitbreiden="uitbreiden" in flags,
                flag_splits_bouwkundig="splits_bouwkundig" in flags,
                flag_splits_kadastraal="splits_kadastraal" in flags,
                flag_splitsvergunning="splitsvergunning" in flags,
                flag_verhuurd="verhuurd" in flags,
                context="Voorbeelddata ter demonstratie van het scoringsmodel.",
            )
            s.add(l)
        s.commit()
        n = s.query(Listing).count()
    compute_scores()
    return n
