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


def send_telegram(text: str, knoppen: list | None = None) -> bool:
    """knoppen: Telegram inline_keyboard, bv. [[{"text": "...", "callback_data": "..."}]]."""
    token, chat = _env("TELEGRAM_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = {"chat_id": chat, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "false"}
    if knoppen:
        import json as _json
        data["reply_markup"] = _json.dumps({"inline_keyboard": knoppen})
    return _post(f"https://api.telegram.org/bot{token}/sendMessage", data)


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
    if o.get("veiling"):
        datum = (o.get("auction_date") or "")[:10]
        prijsregel = (f"🔨 Veiling {datum} · MAX. BOD {_fmt_eur(o.get('max_bod'))} "
                      f"· {o.get('living_area') or '?'} m²  (daarboven haal je je doelrendement niet)")
    else:
        prijsregel = (f"Vraagprijs {_fmt_eur(o.get('price'))} · {o.get('living_area') or '?'} m² "
                      f"· {_fmt_eur(o.get('price_m2'))}/m²")
    regels = [
        prijsregel,
        f"Strategie: {o.get('best_strategie','?')}"
        + ("" if o.get("split_allowed", True) else " (niet splitsbaar)"),
    ]
    # Splitsen is de hoofdstrategie: aantal appartementen + zekerheid erbij
    if o.get("split_status") and o["split_status"] != "nee":
        zeker = {"vergunning": "✅ splitsingsvergunning",
                 "genoemd": "splitsen genoemd in advertentie",
                 "potentieel": "potentieel — check gemeente"}.get(o["split_status"], "")
        regels.append(f"🏢 {'~' if o['split_status'] == 'potentieel' else ''}"
                      f"{o.get('units')} appartementen à ±{o.get('unit_m2')} m² · {zeker}")
    if o.get("ontwikkelproject"):
        regels.append("🏗 Ontwikkelproject (>20 app.) — cijfers alleen indicatief")
    regels += [
        f"Netto conservatief: {_fmt_eur(o.get('best_laag'))} "
        f"(mid {_fmt_eur(o.get('best_mid'))})",
        f"ROI {o.get('roi_laag_pct')}% · marge {o.get('marge_pct')}% "
        f"· flip-score {o.get('flip_score')}",
    ]
    tags = o.get("motivated_tags") or []
    if tags:
        regels.append("🔥 " + ", ".join(tags))
    # Wat het agent-team ervan vond (leeg zolang er geen ANTHROPIC_API_KEY is)
    ag = _agent_oordeel(o.get("id") or 0)
    if ag.get("samenvatting"):
        regels.append(f"📖 Lotte (Lezer): {ag['samenvatting']}")
    if ag.get("advies") == "uitzoeken":
        regels.append("🔍 Kees (Criticus): eerst uitzoeken — zijn vragen staan in het dashboard")
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


def _agent_oordeel(listing_id: int) -> dict:
    """Oordeel van het agent-team bij één object (leeg als het team uit staat)."""
    from .db import Listing, SessionLocal
    try:
        with SessionLocal() as s:
            row = s.get(Listing, listing_id)
            if not row:
                return {}
            return {"advies": row.ai_advies or "", "splits": row.ai_splits or "",
                    "samenvatting": row.ai_samenvatting or ""}
    except Exception:
        return {}


def _agent_ok(o: dict) -> bool:
    a = _agent_oordeel(o.get("id") or 0)
    return a.get("advies") != "laten_lopen"


def check_and_alert(region: str = "", profile: str = "",
                    limit: int = 25) -> dict:
    """Zoek nieuwe topdeals en stuur er een melding over. Idempotent.
    ALERT_PROFILE bepaalt de criteria (bv. 'bob': alleen splitsbaar, tot €1 mln)."""
    if not notify_enabled():
        return {"status": "uit", "reden": "geen TELEGRAM_/PUSHOVER_/SMTP_ instellingen"}

    from .scenarios import get_profile, top_listings
    profile = profile or _env("ALERT_PROFILE", "bob")
    region = region or _env("ALERT_REGION", "focus")
    min_profit = float(_env("ALERT_MIN_PROFIT", "50000"))
    min_roi = float(_env("ALERT_MIN_ROI", "15"))

    data = top_listings(get_profile(profile), n=limit, rank="risk", region=region)
    # Grote projecten niet pushen: cijfers indicatief, en niet 'min moeite'
    kandidaten = [o for o in data["top"]
                  if o.get("categorie") != "project"
                  and (o.get("best_laag") or 0) >= min_profit
                  and (o.get("roi_laag_pct") or 0) >= min_roi]
    if not kandidaten:
        return {"status": "ok", "nieuw": 0, "bekeken": len(data["top"])}

    # Waar het agent-team 'laten lopen' adviseert, geen melding: dat is precies
    # het soort moeite dat we willen vermijden. In het dashboard blijft hij
    # staan, met de bezwaren erbij.
    kandidaten = [o for o in kandidaten if _agent_ok(o)]
    if not kandidaten:
        return {"status": "ok", "nieuw": 0, "bekeken": len(data["top"]),
                "afgeraden_door_criticus": True}

    sent_ids = _already_sent([o["id"] for o in kandidaten])
    nieuw = [o for o in kandidaten if o["id"] not in sent_ids]
    verstuurd = 0
    for o in nieuw:
        titel, body, url = _deal_text(o)
        ok = False
        # Het team werkt op opdracht: één tik op de knop en het zoekt deze deal uit
        knoppen = None
        try:
            from .agents import agents_enabled
            if agents_enabled():
                knoppen = [[{"text": "🔍 Laat het team uitzoeken",
                             "callback_data": f"onderzoek:{o['id']}"}]]
        except Exception:
            pass
        ok |= send_telegram(f"<b>{titel}</b>\n{body}", knoppen=knoppen)
        ok |= send_pushover(titel, body, url)
        ok |= send_email(f"DealRadar: {o.get('address','')} "
                         f"({_fmt_eur(o.get('best_laag'))} netto)",
                         f"{titel}\n\n{body}")
        if ok:
            _mark_sent(o["id"], int(o.get("deal_score") or 0))
            verstuurd += 1
    return {"status": "ok", "nieuw": verstuurd, "kandidaten": len(kandidaten),
            "drempel": {"min_winst": min_profit, "min_roi": min_roi}}


def send_daily_status(report: dict) -> bool:
    """Dagelijks bericht na de ochtendrun: draait hij nog, en wat leverde het op?

    Zo weet je elke ochtend zonder te kijken dat DealRadar nog scrapet — en
    krijg je een seintje als een bron stuk is. Uit te zetten met DAILY_STATUS=0."""
    if _env("DAILY_STATUS", "1") == "0":
        return False
    regels = ["<b>📡 DealRadar — ochtendrun</b>"]
    fout = []
    for bron, r in report.items():
        if bron.startswith("_") or not isinstance(r, dict):
            continue
        if r.get("status") == "ok":
            nieuw = r.get("new", 0)
            wo = r.get("woningen", r.get("found", 0))
            regels.append(f"✅ {bron}: {wo} woningen ({nieuw} nieuw)")
        else:
            fout.append(bron)
            regels.append(f"⚠️ {bron}: {r.get('status')} {str(r.get('message', ''))[:60]}")
    try:
        from .scenarios import get_profile, top_listings
        profiel = _env("ALERT_PROFILE", "bob")
        dp = top_listings(get_profile(profiel), n=1, rank="risk", region="focus", soort="project")
        for soort, kop in (("koop", "🏠 Funda te koop"), ("veiling", "🔨 Veilingen (max. bod)")):
            d = top_listings(get_profile(profiel), n=3, rank="risk", region="focus", soort=soort)
            regels.append(f"\n{kop}: {d['beoordeeld']} splitskansen met winst")
            for o in d["top"]:
                p = (f"max. bod {_fmt_eur(o.get('max_bod'))}" if o.get("veiling")
                     else _fmt_eur(o.get("price")))
                regels.append(f"• {o.get('address')}, {o.get('city')} — {p} · "
                              f"{o.get('units')} app. · netto {_fmt_eur(o.get('best_laag'))}")
        regels.append(f"\n🏗 Grote projecten: {dp['beoordeeld']} (zie dashboard, cijfers indicatief)")
    except Exception as e:
        regels.append(f"(ranglijst niet beschikbaar: {str(e)[:60]})")
    # Agent-team: draait het, blijft het bij, en wat kost het?
    try:
        from .agents import agents_enabled, dagbudget
        if agents_enabled():
            from .agents.team import wachtrij
            from .db import agent_kosten_vandaag, agent_werk_overzicht
            w = agent_werk_overzicht()
            adv = w["advies_verdeling"]
            regels.append(
                f"\n👥 Team: Lotte las {w['gelezen']} objecten"
                f"{f', {wachtrij()} in de wachtrij' if wachtrij() else ' (wachtrij leeg)'}"
                f" · Rik zocht {w['gemeenten']} gemeenten uit"
                f" · ${agent_kosten_vandaag():.2f} van ${dagbudget():.2f} vandaag")
            if adv:
                regels.append("   Kees: " + ", ".join(
                    f"{k.replace('_', ' ')} {n}" for k, n in adv.items()))
    except Exception as e:
        regels.append(f"(agent-overzicht niet beschikbaar: {str(e)[:60]})")

    if fout and len(fout) == sum(1 for b in report if not b.startswith("_")):
        regels.insert(1, "❌ ALLE bronnen faalden — kijk in Railway naar de logs.")
    return send_telegram("\n".join(regels))
