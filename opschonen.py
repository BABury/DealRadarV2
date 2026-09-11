#!/usr/bin/env python3
"""Verwijdert objecten die geen woning zijn uit de database.

NIET-DESTRUCTIEF STANDAARD: zonder --uitvoeren laat hij alleen zien wat er
zou verdwijnen. Draai eerst zonder vlag, controleer de lijst, en pas daarna
met --uitvoeren.

    python opschonen.py              # alleen tonen
    python opschonen.py --uitvoeren  # daadwerkelijk verwijderen
"""
import sys
from collections import Counter

from app.db import SessionLocal, Listing
from app.property_filter import keep, is_residential, is_usable

echt = "--uitvoeren" in sys.argv

with SessionLocal() as s:
    rijen = s.query(Listing).all()
    weg = [(l.id, l.to_dict()) for l in rijen if not keep(l.to_dict())]
    blijft = len(rijen) - len(weg)

    print(f"in database : {len(rijen)}")
    print(f"blijft staan: {blijft} woningen")
    print(f"te verwijderen: {len(weg)}\n")

    print("redenen:")
    for reden, n in Counter(
        "geen data (prijs/oppervlak ontbreekt)" if not is_usable(d)
        else "geen woning" for _, d in weg).most_common():
        print(f"  {n:>5}  {reden}")

    print("\nper bron:")
    for bron, n in Counter(d["source"] for _, d in weg).most_common():
        print(f"  {n:>5}  {bron}")

    print("\nvoorbeelden van wat verdwijnt:")
    for _, d in weg[:10]:
        print(f"  - {(d['address'] or '?')[:44]:<44} type='{(d['property_type'] or '')[:26]}'")

    if not echt:
        print("\n>> PROEFDRAAI — er is niets verwijderd.")
        print(">> Klopt de lijst? Draai dan: python opschonen.py --uitvoeren")
    else:
        ids = [i for i, _ in weg]
        for i in range(0, len(ids), 500):
            s.query(Listing).filter(Listing.id.in_(ids[i:i + 500])).delete(
                synchronize_session=False)
        s.commit()
        print(f"\n>> {len(ids)} objecten verwijderd. Over: {blijft} woningen.")
