"""Agent-team — de leesbare kant van DealRadar.

Waarom dit bestaat: de scraper is goed in cijfers (prijs, m², €/m²) maar blind
voor taal. Juist in de tekst van een advertentie of veilingakte staat wat
bepaalt of splitsen kán: aparte opgang, monumentstatus, een zittende huurder,
of het pand al gesplitst is. Dat leest een taalmodel beter dan een regex.

Harde scheiding, bewust:
  rekenen  -> code (scenarios.py). Exact, controleerbaar, gratis.
  lezen    -> agents. Beoordelen van tekst en beleid, met bronvermelding.

Een agent mag dus nooit een winstbedrag verzinnen; hij levert feiten en
risico's, en de code rekent daarmee.

Zonder ANTHROPIC_API_KEY doet dit hele pakket niets en werkt de app zoals
voorheen. Kosten zijn begrensd met AGENT_MAX_USD_PER_DAY (standaard $2).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

# Haiku leest honderden advertenties voor centen; Sonnet doet het denkwerk
# (gemeentebeleid, kritiek). Overrulen kan met variabelen.
MODEL_LEZER = os.getenv("AGENT_MODEL_LEZER", "claude-haiku-4-5")
MODEL_DENKER = os.getenv("AGENT_MODEL_DENKER", "claude-sonnet-5")

# Prijs per miljoen tokens (USD), alleen voor de kostenteller/rem.
PRIJS = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
_STANDAARD_PRIJS = (3.0, 15.0)

_lock = threading.Lock()
_client = None

# Wat dit proces heeft verbruikt sinds de start (los van de dagteller in de DB).
VERBRUIK = {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "fouten": 0, "laatste": None}

# Live voortgang per agent, zodat je in het dashboard kunt MEEKIJKEN terwijl
# een ronde loopt. Zonder dit weet je alleen achteraf dat er iets gebeurd is.
PROGRESS: dict = {}


def begin(agent: str, totaal: int = 0, wat: str = "") -> None:
    with _lock:
        PROGRESS[agent] = {"bezig": True, "gedaan": 0, "totaal": totaal,
                           "nu": wat, "fouten": 0, "klaar": None,
                           "gestart": dt.datetime.utcnow().isoformat(timespec="seconds")}


def stap(agent: str, nu: str = "", fout: bool = False) -> None:
    with _lock:
        p = PROGRESS.get(agent)
        if not p:
            return
        p["gedaan"] += 1
        if nu:
            p["nu"] = nu
        if fout:
            p["fouten"] += 1


def klaar(agent: str, samenvatting: str = "") -> None:
    with _lock:
        p = PROGRESS.get(agent)
        if not p:
            return
        p["bezig"] = False
        p["nu"] = ""
        p["klaar"] = dt.datetime.utcnow().isoformat(timespec="seconds")
        p["samenvatting"] = samenvatting


class AgentUit(RuntimeError):
    """Geen API-key, of het dagbudget is op."""


# ── Context voor het logboek ─────────────────────────────────────
# Welke ronde loopt er, en over welk object/gemeente gaat de aanroep? Dat
# wordt per thread bijgehouden zodat vraag_json/onderzoek het zelf kunnen
# vastleggen, zonder dat elke agent de administratie hoeft te doen.
_ctx = threading.local()


def context() -> dict:
    return dict(getattr(_ctx, "d", None) or {})


class onderwerp:
    """with onderwerp(listing_id=12, stad='rotterdam', onderwerp='Herenstraat 1'): ..."""

    def __init__(self, **kw):
        self.kw = kw
        self.oud: dict = {}

    def __enter__(self):
        self.oud = context()
        _ctx.d = {**self.oud, **self.kw}
        return self

    def __exit__(self, *exc):
        _ctx.d = self.oud
        return False


def laatste_actie() -> int:
    """Id van de logregel van de laatste modelaanroep in deze thread."""
    return int(context().get("_laatste_actie") or 0)


def _zet_laatste(actie_id: int) -> None:
    d = context()
    d["_laatste_actie"] = actie_id
    _ctx.d = d


def noteer_toegepast(wat: dict) -> None:
    """Legt vast wat de CODE met het antwoord deed (begrenzen, overrulen).
    Juist dat verschil tussen voorstel en toepassing moet zichtbaar zijn."""
    from ..db import actie_update
    actie_update(laatste_actie(), toegepast=wat)


def _log(agent: str, stap: str, model: str, invoer: str, *, antwoord=None,
         bronnen=None, usage=None, duur_ms: int = 0, fout: str = "") -> int:
    from ..db import actie_log
    c = context()
    tok_in = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    tok_out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
    actie_id = actie_log(
        agent=agent, stap=stap, model=model,
        ronde_id=c.get("ronde_id"), listing_id=c.get("listing_id"),
        stad=(c.get("stad") or "")[:100], onderwerp=(c.get("onderwerp") or "")[:300],
        status="fout" if fout else "ok", fout=fout[:2000],
        invoer=invoer, antwoord=antwoord if antwoord is not None else "",
        bronnen=bronnen or [], tok_in=tok_in, tok_out=tok_out,
        usd=_kosten(model, tok_in, tok_out), duur_ms=duur_ms)
    _zet_laatste(actie_id)
    return actie_id


def agents_enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _client_get():
    global _client
    with _lock:
        if _client is None:
            from anthropic import Anthropic
            _client = Anthropic(timeout=float(os.getenv("AGENT_TIMEOUT", "120")))
        return _client


def dagbudget() -> float:
    """In te stellen in het dashboard (standaard $2, of AGENT_MAX_USD_PER_DAY)."""
    from .instellingen import lees
    return float(lees()["dagbudget_usd"])


def _kosten(model: str, tok_in: int, tok_out: int) -> float:
    pi, po = PRIJS.get(model, _STANDAARD_PRIJS)
    return tok_in / 1e6 * pi + tok_out / 1e6 * po


def budget_over() -> float:
    """Hoeveel dollar er vandaag nog te besteden valt."""
    from ..db import agent_kosten_vandaag
    try:
        return max(0.0, dagbudget() - agent_kosten_vandaag())
    except Exception:
        return max(0.0, dagbudget() - VERBRUIK["usd"])


def _boek(model: str, usage, agent: str) -> None:
    tok_in = int(getattr(usage, "input_tokens", 0) or 0)
    tok_out = int(getattr(usage, "output_tokens", 0) or 0)
    usd = _kosten(model, tok_in, tok_out)
    with _lock:
        VERBRUIK["calls"] += 1
        VERBRUIK["in"] += tok_in
        VERBRUIK["out"] += tok_out
        VERBRUIK["usd"] = round(VERBRUIK["usd"] + usd, 4)
        VERBRUIK["laatste"] = dt.datetime.utcnow().isoformat(timespec="seconds")
    try:
        from ..db import boek_agent_kosten
        boek_agent_kosten(agent, usd, tok_in, tok_out)
    except Exception as e:
        print(f"[agents] kosten niet geboekt: {e}", flush=True)


def _blokken_tekst(blokken) -> str:
    out = []
    for b in blokken:
        if getattr(b, "type", "") == "text":
            out.append(b.text)
    return "\n".join(out).strip()


def _opties(model: str) -> dict:
    """Instellingen die per model verschillen.

    - `temperature` bestaat niet meer: de SDK weigert hem en Sonnet 5 ook.
    - Sonnet 5 denkt standaard na (adaptive thinking); `effort` bepaalt hoe
      diep, en dus hoeveel het kost. Haiku 4.5 kent geen `effort`.
    """
    if "haiku" in model:
        return {}
    from .instellingen import lees
    return {"output_config": {"effort": os.getenv("AGENT_EFFORT") or lees()["denkniveau"]}}


def _web_tool(model: str, max_uses: int) -> dict:
    """Nieuwste zoektool voor Sonnet/Opus; de oude variant voor Haiku."""
    typ = os.getenv("AGENT_WEB_SEARCH_TYPE") or (
        "web_search_20250305" if "haiku" in model else "web_search_20260209")
    return {"type": typ, "name": "web_search", "max_uses": max_uses}


def _probleem(r) -> str:
    """Geeft een foutomschrijving als het antwoord onbruikbaar is."""
    sr = getattr(r, "stop_reason", "")
    if sr == "max_tokens":
        return "antwoord afgekapt (max_tokens bereikt)"
    if sr == "refusal":
        det = getattr(r, "stop_details", None)
        return f"geweigerd door het model ({getattr(det, 'category', None) or 'geen categorie'})"
    return ""


def vraag_json(*, agent: str, model: str, system: str, prompt: str,
               schema: dict, naam: str = "antwoord",
               max_tokens: int = 8000) -> dict:
    """Eén antwoord in vast formaat. We forceren een tool-call, want vrije
    tekst 'met JSON erin' gaat vroeg of laat mis en dan valt de pipeline om.

    max_tokens is ruim omdat Sonnet 5 eerst nadenkt en die denktokens ook
    binnen deze grens vallen; je betaalt alleen wat er echt gebruikt wordt.
    """
    if not agents_enabled():
        raise AgentUit("ANTHROPIC_API_KEY ontbreekt")
    if budget_over() <= 0:
        raise AgentUit(f"dagbudget ${dagbudget():.2f} verbruikt")

    import time
    tool = {"name": naam, "description": f"Lever het resultaat van {agent}.",
            "input_schema": schema}
    t0 = time.monotonic()
    try:
        r = _client_get().messages.create(
            model=model, max_tokens=max_tokens,
            system=system, tools=[tool],
            tool_choice={"type": "tool", "name": naam},
            messages=[{"role": "user", "content": prompt}],
            **_opties(model),
        )
    except Exception as e:
        _log(agent, naam, model, prompt, fout=f"{type(e).__name__}: {e}",
             duur_ms=int((time.monotonic() - t0) * 1000))
        raise
    _boek(model, r.usage, agent)
    duur = int((time.monotonic() - t0) * 1000)
    for b in r.content:
        if getattr(b, "type", "") == "tool_use" and b.name == naam:
            data = dict(b.input)
            _log(agent, naam, model, prompt, antwoord=data, usage=r.usage, duur_ms=duur)
            return data
    fout = _probleem(r) or "geen gestructureerd antwoord"
    _log(agent, naam, model, prompt, antwoord=_blokken_tekst(r.content),
         usage=r.usage, duur_ms=duur, fout=fout)
    raise RuntimeError(f"{agent}: {fout}")


def onderzoek(*, agent: str, model: str, system: str, prompt: str,
              max_uses: int = 5, max_tokens: int = 16000) -> tuple[str, list[str]]:
    """Zoekt op internet en geeft (bevindingen, bronnen) als tekst terug.

    Twee stappen, met opzet: eerst zoeken in vrije tekst, daarna in een
    aparte call omzetten naar JSON. Zoeken en formaat forceren tegelijk gaat
    slecht samen. Zonder zoektoegang valt hij terug op eigen kennis en dat
    komt dan als lagere zekerheid terug.

    Lange zoektochten kan de API pauzeren (stop_reason 'pause_turn'); dan
    sturen we het gesprek terug en gaat hij verder waar hij was.
    """
    if not agents_enabled():
        raise AgentUit("ANTHROPIC_API_KEY ontbreekt")
    if budget_over() <= 0:
        raise AgentUit(f"dagbudget ${dagbudget():.2f} verbruikt")

    import time
    from types import SimpleNamespace
    web = [_web_tool(model, max_uses)]
    t0 = time.monotonic()
    stap = "zoeken"
    blokken: list = []
    tok_in = tok_out = 0
    msgs = [{"role": "user", "content": prompt}]
    try:
        for _ in range(2):                       # hooguit één hervatting: grenst tijd én kosten
            r = _client_get().messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=web,
                messages=msgs, **_opties(model))
            _boek(model, r.usage, agent)
            tok_in += int(getattr(r.usage, "input_tokens", 0) or 0)
            tok_out += int(getattr(r.usage, "output_tokens", 0) or 0)
            blokken.extend(r.content)
            if getattr(r, "stop_reason", "") != "pause_turn":
                break
            msgs = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": r.content}]
    except Exception as e:
        print(f"[agents] {agent}: zoeken lukte niet ({str(e)[:120]}) — "
              f"val terug op eigen kennis", flush=True)
        _log(agent, "zoeken", model, prompt, fout=f"zoeken mislukt: {e}",
             duur_ms=int((time.monotonic() - t0) * 1000))
        stap = "zonder_internet"
        t0 = time.monotonic()
        try:
            r = _client_get().messages.create(
                model=model, max_tokens=max_tokens,
                system=system + "\n\nJe hebt GEEN internet. Zeg expliciet wat je "
                                "niet kunt verifiëren en houd de zekerheid laag.",
                messages=[{"role": "user", "content": prompt}], **_opties(model))
        except Exception as e2:
            _log(agent, stap, model, prompt, fout=f"{type(e2).__name__}: {e2}",
                 duur_ms=int((time.monotonic() - t0) * 1000))
            raise
        _boek(model, r.usage, agent)
        tok_in = int(getattr(r.usage, "input_tokens", 0) or 0)
        tok_out = int(getattr(r.usage, "output_tokens", 0) or 0)
        blokken = list(r.content)

    bronnen: list[str] = []
    zoekvragen: list[str] = []
    for b in blokken:
        t = getattr(b, "type", "")
        if t == "server_tool_use":
            q = (getattr(b, "input", None) or {}).get("query")
            if q:
                zoekvragen.append(q)
        if t == "web_search_tool_result":
            inhoud = getattr(b, "content", None)
            # Succes = lijst met resultaten; fout = één object met error_code
            if isinstance(inhoud, list):
                for res in inhoud:
                    u = getattr(res, "url", None)
                    if u and u not in bronnen:
                        bronnen.append(u)
            elif inhoud is not None:
                zoekvragen.append(f"(zoekfout: {getattr(inhoud, 'error_code', inhoud)})")
    verslag = _blokken_tekst(blokken)
    # De zoekvragen horen bij het spoor: je ziet waarop hij gezocht heeft.
    invoer = prompt + ("\n\n[ZOEKVRAGEN VAN DE AGENT]\n- " + "\n- ".join(zoekvragen)
                       if zoekvragen else "")
    fout = "" if verslag else (_probleem(r) or "leeg verslag")
    _log(agent, stap, model, invoer, antwoord=verslag, bronnen=bronnen,
         usage=SimpleNamespace(input_tokens=tok_in, output_tokens=tok_out),
         duur_ms=int((time.monotonic() - t0) * 1000), fout=fout)
    if fout:
        raise RuntimeError(f"{agent}: {fout}")
    return verslag, bronnen


def status() -> dict:
    from ..db import agent_kosten_vandaag
    try:
        vandaag = agent_kosten_vandaag()
    except Exception:
        vandaag = VERBRUIK["usd"]
    return {
        "aan": agents_enabled(),
        "modellen": {"lezer": MODEL_LEZER, "denker": MODEL_DENKER},
        "dagbudget_usd": dagbudget(),
        "vandaag_usd": round(vandaag, 4),
        "over_usd": round(max(0.0, dagbudget() - vandaag), 4),
        "deze_sessie": dict(VERBRUIK),
    }


def kort(tekst: str | None, maxlen: int = 6000) -> str:
    """Advertentieteksten kunnen lang zijn; wij betalen per token."""
    t = (tekst or "").strip()
    return t if len(t) <= maxlen else t[:maxlen] + " […]"


def als_json(x) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))
