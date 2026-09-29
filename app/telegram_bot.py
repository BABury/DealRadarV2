"""Telegram: het team opdrachten geven vanaf je telefoon.

Het team doet niets uit zichzelf. Via Telegram geef je de opdracht:

  🔍-knop onder een dealmelding    het team zoekt dat object uit
  /onderzoek Herenstraat 1         zoekt het object op adres (of plak een link)
  /ronde                           een ronde over de hele wachtrij
  /status                          wat het team doet en wat het vandaag kostte

Railway ontvangt de berichten via een webhook. Veiligheid:
  - Telegram stuurt een geheim mee (afgeleid van je bottoken); zonder dat
    geheim wordt een bericht genegeerd;
  - alleen jouw chat (TELEGRAM_CHAT_ID) krijgt antwoord, de rest wordt stil
    genegeerd.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import threading
import urllib.request


def _token() -> str:
    return (os.getenv("TELEGRAM_TOKEN") or "").strip()


def _chat() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def geheim() -> str:
    """Webhook-geheim, afgeleid van het bottoken: geen extra variabele nodig."""
    return hashlib.sha256(("dealradar-webhook:" + _token()).encode()).hexdigest()[:48]


def publieke_url() -> str:
    dom = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    return os.getenv("PUBLIC_URL") or (f"https://{dom}" if dom else "")


def _api(methode: str, data: dict) -> dict:
    if not _token():
        return {"ok": False, "description": "geen TELEGRAM_TOKEN"}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_token()}/{methode}",
        data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:
        print(f"[telegram] {methode} mislukt: {e}", flush=True)
        return {"ok": False, "description": str(e)[:200]}


def stuur(tekst: str, knoppen: list | None = None, chat: str | None = None) -> bool:
    data = {"chat_id": chat or _chat(), "text": tekst[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if knoppen:
        data["reply_markup"] = {"inline_keyboard": knoppen}
    return bool(_api("sendMessage", data).get("ok"))


def koppel_webhook() -> dict:
    """Vertelt Telegram waar de berichten heen moeten (bij elke start)."""
    url = publieke_url()
    if not (_token() and url):
        return {"ok": False, "description": "TELEGRAM_TOKEN of publieke URL ontbreekt"}
    r = _api("setWebhook", {"url": f"{url}/api/telegram/webhook", "secret_token": geheim(),
                            "allowed_updates": ["message", "callback_query"],
                            "drop_pending_updates": True})
    print(f"[telegram] webhook {url}/api/telegram/webhook: {r.get('ok')} {r.get('description', '')}",
          flush=True)
    # Commando's zichtbaar maken in het menu van de bot
    _api("setMyCommands", {"commands": [
        {"command": "onderzoek", "description": "Laat het team een object uitzoeken (adres of link)"},
        {"command": "ronde", "description": "Laat het team de wachtrij afwerken"},
        {"command": "status", "description": "Wat doet het team, wat kostte het"},
        {"command": "stop", "description": "Stop wat het team nu doet"},
        {"command": "uit", "description": "Hoofdschakelaar uit: niets kan het team starten"},
        {"command": "aan", "description": "Hoofdschakelaar weer aan"},
        {"command": "help", "description": "Uitleg"}]})
    return r


# ── Berichten verwerken ──────────────────────────────────────────

HULP = ("<b>DealRadar-team</b>\n"
        "Het team werkt alleen op jouw opdracht.\n\n"
        "🔍 <b>/onderzoek</b> Herenstraat 1 — Lotte leest, Rik checkt het beleid, Kees zoekt de zwakke plekken. "
        "Je kunt ook een Funda- of veilinglink plakken.\n"
        "🔁 <b>/ronde</b> — het team werkt de wachtrij in je focussteden af\n"
        "📊 <b>/status</b> — wat het team doet en wat het vandaag kostte\n"
        "⏹ <b>/stop</b> — stop wat het team nu doet\n"
        "⏸ <b>/uit</b> · <b>/aan</b> — hoofdschakelaar\n\n"
        "<b>Scrapen op afstand</b>\n"
        "🏠 <b>/scrape</b> — Funda afzoeken in je focussteden\n"
        "🏠 <b>/scrape amsterdam</b> · <b>/scrape amsterdam,rotterdam 140</b> — precies deze steden "
        "(het getal is het paginabudget; boven ±200 per half uur gaat Funda blokkeren)\n"
        "⏹ <b>/scrapestop</b> — stop de lopende scrape\n\n"
        "<b>Kansen bekijken</b>\n"
        "🔺 <b>/kansen</b> — de tien hoogste scores\n"
        "🏚 <b>/vliering</b> — panden met onbenutte ruimte boven (bergzolder, vliering)\n\n"
        "<b>Knoppen omzetten</b>\n"
        "⚙️ <b>/instellingen</b> — alles wat je kunt bijstellen\n"
        "⚙️ <b>/zet</b> vliering_min_m2 25 — één waarde wijzigen\n"
        "🏙 <b>/steden</b> amsterdam,rotterdam,eindhoven — waar we op focussen\n\n"
        "Onder elke dealmelding staat ook een knop om die deal te laten uitzoeken.")


UIT_TEKST = "⏸ Het team staat uit (hoofdschakelaar). Stuur /aan om het weer aan te zetten."


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _eur(v) -> str:
    try:
        return "€" + f"{round(float(v)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def zoek(term: str) -> list[dict]:
    """Objecten die bij een adres of link passen (max 6)."""
    from sqlalchemy import func, or_

    from .db import Listing, SessionLocal
    term = (term or "").strip()
    if not term:
        return []
    with SessionLocal() as s:
        q = s.query(Listing).filter(Listing.is_demo.is_(False))
        if term.startswith("http"):
            basis = term.split("?")[0].rstrip("/")
            rijen = q.filter(or_(Listing.url == term, Listing.url.like(f"{basis}%"))).limit(6).all()
        else:
            rijen = q.filter(Listing.address.ilike(f"%{term}%")).limit(6).all()
            if not rijen and " " in term:
                # "Herenstraat 1 Rotterdam": laatste woord als stad proberen
                adres, stad = term.rsplit(" ", 1)
                rijen = (q.filter(Listing.address.ilike(f"%{adres}%"),
                                  func.lower(Listing.city).like(f"%{stad.lower()}%"))
                         .limit(6).all())
        return [{"id": r.id, "adres": r.address, "stad": r.city, "prijs": r.price,
                 "m2": r.living_area} for r in rijen]


def top_kansen(alleen_vliering: bool = False, n: int = 10) -> list[dict]:
    """De beste objecten in je focussteden, voor onderweg op je telefoon."""
    from sqlalchemy import desc, or_

    from .agents.instellingen import focus_varianten, lees
    from .db import Listing, SessionLocal
    from sqlalchemy import func as _f
    with SessionLocal() as s:
        q = (s.query(Listing).filter(Listing.is_demo.is_(False))
             .filter(_f.lower(_f.trim(Listing.city)).in_(sorted(focus_varianten()))))
        if alleen_vliering:
            drempel = lees()["vliering_min_m2"]
            q = q.filter(or_(Listing.overige_inpandig >= drempel,
                             Listing.flag_vliering.is_(True)))
            q = q.order_by(desc(Listing.overige_inpandig), desc(Listing.flip_score))
        else:
            q = q.order_by(desc(Listing.flip_score))
        return [r.to_dict() for r in q.limit(n).all()]


def kansen_tekst(rijen: list[dict], titel: str) -> tuple[str, list]:
    """Tekst + knoppen, zodat je vanuit de lijst meteen kunt laten uitzoeken."""
    if not rijen:
        return (f"<b>{titel}</b>\nNog niets gevonden. Draai eerst /scrape.", [])
    regels = [f"<b>{titel}</b>"]
    knoppen = []
    for r in rijen:
        v = r.get("vliering") or {}
        extra = ""
        if r.get("overige_inpandig"):
            extra = f" · <b>{r['overige_inpandig']:.0f} m² onbenut</b>"
        if v.get("netto"):
            extra += f" → ca. {_eur(v['netto'])} netto"
        regels.append(f"\n{r.get('flip_score', 0)} · <b>{_e(r.get('address'))}</b>, "
                      f"{_e(r.get('city'))}\n{_eur(r.get('price'))} · "
                      f"{(r.get('living_area') or 0):.0f} m²{extra}")
        knoppen.append([{"text": f"🔍 {r.get('address')}"[:60],
                         "callback_data": f"onderzoek:{r.get('id')}"}])
    return ("\n".join(regels), knoppen[:8])


def instellingen_tekst() -> str:
    from .agents.instellingen import GRENZEN, lees
    ins = lees()
    regels = ["<b>Instellingen</b>", "Wijzigen met <code>/zet sleutel waarde</code>\n"]
    for k, v in ins.items():
        if isinstance(v, list):
            v = ", ".join(v)
        grens = GRENZEN.get(k)
        achter = f"  <i>({grens[0]}–{grens[1]})</i>" if grens else ""
        regels.append(f"<code>{k}</code> = <b>{_e(v)}</b>{achter}")
    return "\n".join(regels)


def zet_instelling(rest: str) -> str:
    """/zet sleutel waarde — één waarde bijstellen, altijd binnen de grenzen."""
    from .agents.instellingen import GRENZEN, STANDAARD, lees, opslaan
    deel = (rest or "").replace("=", " ").split()
    if len(deel) < 2:
        return ("Gebruik: <code>/zet sleutel waarde</code>, bijvoorbeeld "
                "<code>/zet vliering_min_m2 25</code>. Stuur /instellingen voor de lijst.")
    sleutel, waarde = deel[0].strip().lower(), " ".join(deel[1:]).strip()
    if sleutel not in STANDAARD:
        return f"<code>{_e(sleutel)}</code> ken ik niet. Stuur /instellingen voor de lijst."
    if sleutel == "steden":
        return zet_steden(waarde)
    std = STANDAARD[sleutel]
    try:
        if isinstance(std, bool):
            nieuw = waarde.lower() in ("1", "aan", "true", "ja", "on")
        elif isinstance(std, float):
            nieuw = float(waarde.replace(",", "."))
        elif isinstance(std, int):
            nieuw = int(float(waarde.replace(",", ".")))
        else:
            nieuw = waarde
    except ValueError:
        return f"<b>{_e(waarde)}</b> is geen geldige waarde voor <code>{_e(sleutel)}</code>."
    opslaan({sleutel: nieuw})
    na = lees()[sleutel]
    grens = GRENZEN.get(sleutel)
    if grens and na != nieuw:
        return (f"✅ <code>{_e(sleutel)}</code> staat nu op <b>{_e(na)}</b> "
                f"(begrensd op {grens[0]}–{grens[1]}).")
    return f"✅ <code>{_e(sleutel)}</code> staat nu op <b>{_e(na)}</b>."


def zet_steden(rest: str) -> str:
    from .agents.instellingen import lees, opslaan, stad
    steden = [stad(x) for x in (rest or "").replace(";", ",").split(",") if x.strip()]
    if not steden:
        return ("Gebruik: <code>/steden amsterdam,rotterdam,eindhoven</code>.\n"
                f"Nu: <b>{', '.join(lees()['steden'])}</b>")
    opslaan({"steden": steden})
    nu = lees()["steden"]
    return (f"✅ Focus staat nu op <b>{', '.join(nu)}</b>.\n"
            "Dat geldt meteen voor de scraper, de lijst, de meldingen en het team.")


def rapport_tekst(r: dict) -> str:
    """Het resultaat van een onderzoek, compact voor Telegram."""
    if r.get("status") == "al bezig":
        return "⏳ Het team is al met iets anders bezig. Probeer het over een paar minuten opnieuw."
    if r.get("status") == "gestopt":
        return (f"⏹ Onderzoek van <b>{_e(r.get('adres'))}</b> gestopt op jouw verzoek. "
                f"Kosten tot dan: ${r.get('kosten_usd', 0):.3f}.")
    if r.get("status") in ("uit", "budget_op", "niet_gevonden", "error"):
        return f"⚠️ Onderzoek niet gelukt: {_e(r.get('reden') or r.get('message') or r.get('status'))}"
    lt, rk, ks = r.get("lotte") or {}, r.get("rik") or {}, r.get("kees") or {}
    regels = [f"<b>🔍 {_e(r.get('adres'))}, {_e(r.get('stad'))}</b>",
              f"Score {_e(r.get('score_voor'))} → <b>{_e(r.get('score_na'))}</b>"]
    if lt.get("samenvatting"):
        units = f" ({lt['units']} app.)" if lt.get("units") else ""
        regels.append(f"\n📖 <b>Lotte</b>: splitsbaar {_e(lt.get('splitsbaar') or '?')}{_e(units)}\n{_e(lt['samenvatting'])}")
    elif (r.get("lezer") or {}).get("geen_tekst"):
        regels.append("\n📖 <b>Lotte</b>: geen omschrijving beschikbaar om te lezen")
    if rk.get("toegestaan"):
        regels.append(f"\n⚖️ <b>Rik</b>: splitsen {_e(str(rk['toegestaan']).replace('_', ' '))} "
                      f"(zekerheid {_e(rk.get('zekerheid'))})\n{_e(rk.get('samenvatting'))}")
    if ks.get("advies"):
        regels.append(f"\n🔎 <b>Kees</b>: {_e(str(ks['advies']).replace('_', ' '))}\n{_e(ks.get('samenvatting'))}")
        for v in (ks.get("rode_vlaggen") or [])[:3]:
            if isinstance(v, dict):
                regels.append(f"  • {_e(v.get('punt'))} <i>({_e(v.get('ernst'))})</i>")
        vragen = ks.get("eerst_uitzoeken") or []
        if vragen:
            regels.append("Eerst uitzoeken:\n" + "\n".join(f"  ❓ {_e(v)}" for v in vragen[:3]))
    fouten = [n for n in ("lezer", "regelchecker", "criticus")
              if (r.get(n) or {}).get("fouten") or (r.get(n) or {}).get("fout")]
    if fouten:
        regels.append(f"\n⚠️ Niet alles lukte ({', '.join(fouten)}) — zie het logboek.")
    url = publieke_url()
    spoor = f" · <a href=\"{url}/?spoor={r.get('listing_id')}\">volledig spoor</a>" if url else ""
    regels.append(f"\n<i>kosten ${r.get('kosten_usd', 0):.3f} · ronde {_e(r.get('ronde_id'))}</i>{spoor}")
    return "\n".join(regels)


def verwerk(update: dict, start_onderzoek, start_ronde, start_scrape=None) -> None:
    """Eén bericht of knopdruk van Telegram. Werk dat lang duurt gaat naar een
    achtergrondthread; Telegram krijgt meteen antwoord."""
    cb = update.get("callback_query")
    if cb:
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
        if chat != _chat():
            return
        from .agents import hoofdschakelaar_aan
        if not hoofdschakelaar_aan():
            _api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Team staat uit"})
            stuur(UIT_TEKST)
            return
        _api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Team gaat aan de slag"})
        data = cb.get("data") or ""
        if data.startswith("onderzoek:"):
            try:
                lid = int(data.split(":", 1)[1])
            except ValueError:
                return
            _start(lid, start_onderzoek)
        return

    msg = update.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    if chat != _chat():
        return                                   # vreemde chat: stil negeren
    tekst = (msg.get("text") or "").strip()
    laag = tekst.lower()
    if not tekst or laag in ("/start", "/help", "help"):
        stuur(HULP)
        return
    if laag.startswith("/status"):
        stuur(_status_tekst())
        return
    if laag.startswith("/stop"):
        from .agents import stop_aanvragen
        stop_aanvragen()
        stuur("⏹ Stop gegeven. Het team maakt de aanroep af die al onderweg is en houdt dan op.")
        return
    if laag.startswith("/uit") or laag.startswith("/aan"):
        from .agents import stop_aanvragen
        from .agents.instellingen import opslaan
        aan = laag.startswith("/aan")
        opslaan({"team_aan": aan})
        if not aan:
            stop_aanvragen()
        stuur("✅ Team staat <b>aan</b> — het werkt op jouw opdracht." if aan else
              "⏸ Team staat <b>uit</b>. Niets kan het nog starten tot je /aan stuurt.")
        return
    # ── scrapen op afstand ──
    if laag.startswith("/scrapestop"):
        from .scrapers.stop import stop_aanvragen
        stop_aanvragen()
        stuur("⏹ Stop gegeven. De pagina die onderweg is wordt afgemaakt, daarna stopt de scrape.")
        return
    if laag.startswith("/scrape"):
        if start_scrape is None:
            stuur("Scrapen via Telegram is in deze versie nog niet beschikbaar.")
            return
        rest = tekst[len("/scrape"):].strip()
        budget, steden = 0, []
        for stuk in rest.replace(";", ",").replace(" ", ",").split(","):
            stuk = stuk.strip()
            if not stuk:
                continue
            if stuk.isdigit():
                budget = int(stuk)
            else:
                from .agents.instellingen import stad
                steden.append(stad(stuk))
        from .agents.instellingen import lees
        doel = steden or lees()["steden"]
        stuur(f"🏠 Ik zet Funda aan het werk voor <b>{', '.join(doel)}</b>"
              + (f" met een budget van {budget} pagina's" if budget else "")
              + ".\nDit duurt een paar minuten per stad. Stop kan met /scrapestop.")
        threading.Thread(target=start_scrape, args=(steden or None, budget or None),
                         daemon=True).start()
        return

    # ── kansen bekijken ──
    if laag.startswith("/vliering") or laag.startswith("/kansen"):
        alleen_vliering = laag.startswith("/vliering")
        titel = ("Onbenutte ruimte boven" if alleen_vliering else "Hoogste scores")
        try:
            rijen = top_kansen(alleen_vliering)
        except Exception as e:
            stuur(f"Kon de lijst niet ophalen: {_e(str(e)[:120])}")
            return
        tekst_uit, knoppen = kansen_tekst(rijen, titel)
        stuur(tekst_uit, knoppen=knoppen or None)
        return

    # ── knoppen omzetten ──
    if laag.startswith("/herlees"):
        from .keywords import analyse_description
        from .db import Listing, SessionLocal
        n, bij = 0, 0
        with SessionLocal() as sess:
            for r in sess.query(Listing).filter(Listing.omschrijving.isnot(None),
                                                Listing.omschrijving != "").all():
                n += 1
                an = analyse_description(r.omschrijving or "")
                if any(k.startswith("flag_") and hasattr(r, k) and getattr(r, k) != v
                       for k, v in an.items()):
                    for k, v in an.items():
                        if k.startswith("flag_") and hasattr(r, k):
                            setattr(r, k, v)
                    bij += 1
            sess.commit()
        from .scoring import compute_scores
        compute_scores()
        stuur(f"🔁 {n} omschrijvingen opnieuw gelezen, <b>{bij}</b> bijgewerkt. "
              "Stuur /vliering voor het resultaat.")
        return
    if laag.startswith("/instellingen"):
        stuur(instellingen_tekst())
        return
    if laag.startswith("/zet"):
        stuur(zet_instelling(tekst[len("/zet"):].strip()))
        return
    if laag.startswith("/steden"):
        stuur(zet_steden(tekst[len("/steden"):].strip()))
        return

    if laag.startswith("/ronde"):
        from .agents import hoofdschakelaar_aan
        if not hoofdschakelaar_aan():
            stuur(UIT_TEKST)
            return
        stuur("🔁 Het team begint aan een ronde over de wachtrij in je focussteden. Ik meld me als het klaar is.")
        threading.Thread(target=start_ronde, daemon=True).start()
        return
    term = tekst
    for voor in ("/onderzoek", "onderzoek", "zoek uit", "uitzoeken"):
        if laag.startswith(voor):
            term = tekst[len(voor):].strip(" :")
            break
    else:
        if not tekst.startswith("http"):
            stuur("Dat begrijp ik niet. " + HULP)
            return
    gevonden = zoek(term)
    if not gevonden:
        stuur(f"Geen object gevonden voor <i>{_e(term)}</i>. Probeer een deel van het adres, bv. "
              f"<code>/onderzoek Herenstraat 1</code>.")
    elif len(gevonden) == 1:
        _start(gevonden[0]["id"], start_onderzoek)
    else:
        stuur(f"Ik vond {len(gevonden)} objecten. Welke moet het team uitzoeken?",
              knoppen=[[{"text": f"{g['adres']}, {g['stad']} · {_eur(g['prijs'])}"[:60],
                         "callback_data": f"onderzoek:{g['id']}"}] for g in gevonden])


def _start(listing_id: int, start_onderzoek) -> None:
    from .agents import hoofdschakelaar_aan
    from .db import Listing, SessionLocal
    if not hoofdschakelaar_aan():
        stuur(UIT_TEKST)
        return
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        naam = f"{row.address}, {row.city}" if row else f"object {listing_id}"
    stuur(f"🔍 Het team zoekt <b>{_e(naam)}</b> uit: Rik checkt het beleid, Lotte leest, Kees zoekt "
          f"de zwakke plekken. Meestal binnen 1–3 minuten.")
    threading.Thread(target=start_onderzoek,
                     args=(listing_id, "telegram", lambda r: stuur(rapport_tekst(r))),
                     daemon=True).start()


def _status_tekst() -> str:
    try:
        from .agents import agents_enabled, budget_over, dagbudget
        from .agents.instellingen import lees
        from .agents.team import wachtrij
        from .db import agent_kosten_vandaag
        if not agents_enabled():
            return "🤖 Het team staat uit: er is geen ANTHROPIC_API_KEY ingesteld."
        ins = lees()
        return (f"📊 <b>Team</b> — {'aan' if ins['team_aan'] else '⏸ UIT (hoofdschakelaar)'}\n"
                f"Wachtrij Lotte: {wachtrij()} objecten in {len(ins['steden'])} focussteden\n"
                f"Vandaag: ${agent_kosten_vandaag():.2f} van ${dagbudget():.2f} "
                f"(nog ${budget_over():.2f})\n"
                f"Automatische rondes: {'aan' if ins['automatische_rondes'] else 'uit — alleen op opdracht'}")
    except Exception as e:
        return f"Status niet op te halen: {_e(e)}"
