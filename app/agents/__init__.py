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
MODEL_LEZER = os.getenv("AGENT_MODEL_LEZER", "claude-haiku-4-5-20251001")
MODEL_DENKER = os.getenv("AGENT_MODEL_DENKER", "claude-sonnet-5")

# Prijs per miljoen tokens (USD), alleen voor de kostenteller/rem.
PRIJS = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (15.0, 75.0),
}
_STANDAARD_PRIJS = (3.0, 15.0)

_lock = threading.Lock()
_client = None

# Wat dit proces heeft verbruikt sinds de start (los van de dagteller in de DB).
VERBRUIK = {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "fouten": 0, "laatste": None}


class AgentUit(RuntimeError):
    """Geen API-key, of het dagbudget is op."""


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
    return float(os.getenv("AGENT_MAX_USD_PER_DAY", "2.0"))


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


def vraag_json(*, agent: str, model: str, system: str, prompt: str,
               schema: dict, naam: str = "antwoord",
               max_tokens: int = 1500) -> dict:
    """Eén antwoord in vast formaat. We forceren een tool-call, want vrije
    tekst 'met JSON erin' gaat vroeg of laat mis en dan valt de pipeline om."""
    if not agents_enabled():
        raise AgentUit("ANTHROPIC_API_KEY ontbreekt")
    if budget_over() <= 0:
        raise AgentUit(f"dagbudget ${dagbudget():.2f} verbruikt")

    tool = {"name": naam, "description": f"Lever het resultaat van {agent}.",
            "input_schema": schema}
    r = _client_get().messages.create(
        model=model, max_tokens=max_tokens, temperature=0,
        system=system, tools=[tool],
        tool_choice={"type": "tool", "name": naam},
        messages=[{"role": "user", "content": prompt}],
    )
    _boek(model, r.usage, agent)
    for b in r.content:
        if getattr(b, "type", "") == "tool_use" and b.name == naam:
            return dict(b.input)
    raise RuntimeError(f"{agent}: geen structureel antwoord ontvangen")


def onderzoek(*, agent: str, model: str, system: str, prompt: str,
              max_uses: int = 5, max_tokens: int = 3000) -> tuple[str, list[str]]:
    """Zoekt op internet en geeft (bevindingen, bronnen) als tekst terug.

    Twee stappen, met opzet: eerst zoeken in vrije tekst, daarna in een
    aparte call omzetten naar JSON. Zoeken en formaat forceren tegelijk gaat
    slecht samen. Zonder zoektoegang valt hij terug op eigen kennis en dat
    komt dan als lagere zekerheid terug.
    """
    if not agents_enabled():
        raise AgentUit("ANTHROPIC_API_KEY ontbreekt")
    if budget_over() <= 0:
        raise AgentUit(f"dagbudget ${dagbudget():.2f} verbruikt")

    web = [{"type": "web_search_20250305", "name": "web_search",
            "max_uses": max_uses}]
    try:
        r = _client_get().messages.create(
            model=model, max_tokens=max_tokens, temperature=0,
            system=system, tools=web,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[agents] {agent}: zoeken lukte niet ({str(e)[:120]}) — "
              f"val terug op eigen kennis", flush=True)
        r = _client_get().messages.create(
            model=model, max_tokens=max_tokens, temperature=0,
            system=system + "\n\nJe hebt GEEN internet. Zeg expliciet wat je "
                            "niet kunt verifiëren en houd de zekerheid laag.",
            messages=[{"role": "user", "content": prompt}],
        )
    _boek(model, r.usage, agent)

    bronnen: list[str] = []
    for b in r.content:
        if getattr(b, "type", "") == "web_search_tool_result":
            for res in (getattr(b, "content", None) or []):
                u = getattr(res, "url", None)
                if u and u not in bronnen:
                    bronnen.append(u)
    return _blokken_tekst(r.content), bronnen


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
