"""Telefoonmeldingen bij een goede kans.

Ondersteunt (kies er één, of meerdere tegelijk):
  • Telegram  — TELEGRAM_TOKEN + TELEGRAM_CHAT_ID   (gratis, snel op te zetten)
  • Pushover  — PUSHOVER_TOKEN + PUSHOVER_USER      (native iOS/Android push)
  • E-mail    — SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_EMAIL

Een deal wordt gemeld als hij door het risicofilter komt én:
  conservatieve nettowinst >= ALERT_MIN_PROFIT  (default 50.000)
  en conservatieve ROI     >= ALERT_MIN_ROI     (default 15%)

Elke deal wordt maar één keer gemeld (bijgehouden in tabel alerts_sent).
"""
from __future__ import annotations

import datetime as dt
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _post(url: str, data: dict, timeout: int = 15) -> bool:
    try:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[notify] versturen mislukt ({url.split('/')[2]}): {e}", flush=True)
        return False


def send_telegram(text: str) -> bool:
    token, chat = _env("TELEGRAM_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    return _post(f"https://api.telegram.org/bot{token}/sendMessage",
                 {"chat_id": chat, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": "false"})


def send_pushover(title: str, text: str, url: str = "") -> bool:
    token, user = _env("PUSHOVER_TOKEN"), _env("PUSHOVER_USER")
    if not token or not user:
        return False
    data = {"token": token, "user": user, "title": title, "message": text}
    if url:
        data["url"] = url
        data["url_title"] = "Bekijk object"
    return _post("https://api.pushover.net/1/messages.json", data)


def send_email(subject: str, text: str) -> bool:
    host, user, pw = _env("SMTP_HOST"), _env("SMTP_USER"), _env("SMTP_PASS")
    to = _env("ALERT_EMAIL") or user
    if not host or not user or not pw or not to:
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        msg.set_content(text)
        port = int(_env("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[notify] e-mail mislukt: {e}", flush=True)
        return False


def notify_enabled() -> bool:
    return bool(_env("TELEGRAM_TOKEN") or _env("PUSHOVER_TOKEN") or _env("SMTP_HOST"))


def _fmt_eur(v) -> str:
    try:
        return "€" + f"{round(float(v)):,}".replace(",", ".")
    except Exception:
        return "—"


def _deal_text(o: dict) -> tuple[str, str, str]:
    titel = f"🏠 {o.get('address','?')} · {o.get('city','')}"
    regels = [
        f"Vraagprijs {_fmt_eur(o.get('price'))} · {o.get('living_area') or '?'} m² "
        f"· {_fmt_eur(o.get('price_m2'))}/m²",
        f"Strategie: {o.get('best_strategie','?')}"
        + ("" if o.get("split_allowed", True) else " (niet splitsbaar)"),
        f"Netto conservatief: {_fmt_eur(o.get('best_laag'))} "
        f"(mid {_fmt_eur(o.get('best_mid'))})",
        f"ROI {o.get('roi_laag_pct')}% · marge {o.get('marge_pct')}% "
        f"· flip-score {o.get('flip_score')}",
    ]
    tags = o.get("motivated_tags") or []
    if tags:
        regels.append("🔥 " + ", ".join(tags))
    url = o.get("url") or ""
    if url:
        regels.append(url)
    return titel, "\n".join(regels), url


def _already_sent(ids: list[int]) -> set[int]:
    from .db import AlertSent, SessionLocal
    if not ids:
        return set()
    with SessionLocal() as s:
        return {a.listing_id for a in
                s.query(AlertSent).filter(AlertSent.listing_id.in_(ids)).all()}


def _mark_sent(listing_id: int, score: int) -> None:
    from .db import AlertSent, SessionLocal
    with SessionLocal() as s:
        s.add(AlertSent(listing_id=listing_id, deal_score=score,
                        sent=dt.datetime.utcnow()))
        s.commit()


def check_and_alert(region: str = "", profile: str = "standaard",
                    limit: int = 25) -> dict:
    """Zoek nieuwe topdeals en stuur er een melding over. Idempotent."""
    if not notify_enabled():
        return {"status": "uit", "reden": "geen TELEGRAM_/PUSHOVER_/SMTP_ instellingen"}

    from .scenarios import get_profile, top_listings
    region = region or _env("ALERT_REGION", "grote_steden")
    min_profit = float(_env("ALERT_MIN_PROFIT", "50000"))
    min_roi = float(_env("ALERT_MIN_ROI", "15"))

    data = top_listings(get_profile(profile), n=limit, rank="risk", region=region)
    kandidaten = [o for o in data["top"]
                  if (o.get("best_laag") or 0) >= min_profit
                  and (o.get("roi_laag_pct") or 0) >= min_roi]
    if not kandidaten:
        return {"status": "ok", "nieuw": 0, "bekeken": len(data["top"])}

    sent_ids = _already_sent([o["id"] for o in kandidaten])
    nieuw = [o for o in kandidaten if o["id"] not in sent_ids]
    verstuurd = 0
    for o in nieuw:
        titel, body, url = _deal_text(o)
        ok = False
        ok |= send_telegram(f"<b>{titel}</b>\n{body}")
        ok |= send_pushover(titel, body, url)
        ok |= send_email(f"DealRadar: {o.get('address','')} "
                         f"({_fmt_eur(o.get('best_laag'))} netto)",
                         f"{titel}\n\n{body}")
        if ok:
            _mark_sent(o["id"], int(o.get("deal_score") or 0))
            verstuurd += 1
    return {"status": "ok", "nieuw": verstuurd, "kandidaten": len(kandidaten),
            "drempel": {"min_winst": min_profit, "min_roi": min_roi}}
