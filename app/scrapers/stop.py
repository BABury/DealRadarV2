"""Stopknop voor een lopende scrape.

Zelfde gedachte als de stopknop van het agent-team: de pagina die al onderweg
is wordt netjes afgemaakt, daarna stopt de scraper. Nooit een proces hard
afbreken — dan blijft er een browser hangen op Railway.
"""
from __future__ import annotations

import threading

_stop = threading.Event()


def stop_aanvragen() -> None:
    _stop.set()


def stop_wissen() -> None:
    _stop.clear()


def stop_gevraagd() -> bool:
    return _stop.is_set()
