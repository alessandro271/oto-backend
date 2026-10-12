"""F-049 — la clé Browserbase n'atteint jamais `tool_calls.error`.

La `connect_url` CDP porte `?apiKey=<BROWSERBASE_API_KEY>` (la `connectUrl` d'une
session, `?signingKey=`). Quand Playwright échoue à s'y brancher, son message
recopie l'URL ENTIÈRE ; ce message partait dans `tool_calls.error` (lu par les
admins d'org) et, via les outils qui interpolent l'erreur, jusqu'à l'agent.

Deux niveaux, deux tests :
- à la SOURCE : `browserbase.connect_cdp` relève une `BrowserbaseError` propre ;
- en PROFONDEUR : `db.insert_tool_call` masque toute URL à secret d'un `error`,
  quel que soit l'outil qui l'a produit.
"""
from __future__ import annotations

import pytest

from oto_mcp import browserbase
from oto_mcp import journal_secrets as js

FAUSSE_CLE = "bb_live_FAUSSECLE0123456789abcdef"
FAUX_SIGNING = "sk_FAUXSIGNING9876543210zyx"


def _erreur_playwright(url: str) -> str:
    """La forme réelle d'un échec `connect_over_cdp` : l'URL en tête ET dans le
    journal d'appel."""
    return (f"BrowserType.connect_over_cdp: WebSocket error: {url} 410 Gone\n"
            "Call log:\n"
            f"  - <ws connecting> {url}\n"
            "  - <ws unexpected response> 410 Gone")


class _Chromium:
    def __init__(self, message: str):
        self.message = message
        self.urls: list[str] = []

    async def connect_over_cdp(self, url):
        from patchright.async_api import Error
        self.urls.append(url)
        raise Error(self.message or _erreur_playwright(url))


class _Playwright:
    def __init__(self, message: str = ""):
        self.chromium = _Chromium(message)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def cle(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", FAUSSE_CLE)
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    return FAUSSE_CLE


def _capturer_insert(monkeypatch) -> dict:
    from oto_mcp.db import usage

    captured: dict = {}

    class _Conn:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(usage, "_connect", lambda: _Conn())
    return captured


# --- le masque --------------------------------------------------------------- #

@pytest.mark.parametrize("texte, secret", [
    (f"wss://connect.browserbase.com?apiKey={FAUSSE_CLE}&sessionId=s1", FAUSSE_CLE),
    (f"wss://connect.browserbase.com?signingKey={FAUX_SIGNING}", FAUX_SIGNING),
    (f"GET https://api.x.io/v1?api_key={FAUSSE_CLE} failed", FAUSSE_CLE),
    (f"err: https://h.io/p?x=1&token={FAUSSE_CLE}", FAUSSE_CLE),
    (f"err: https://h.io/p?key={FAUSSE_CLE}", FAUSSE_CLE),
    (f"postgres://admin:{FAUSSE_CLE}@db.internal:5432/oto refused", FAUSSE_CLE),
    (f"wss://{FAUSSE_CLE}@connect.example.com/devtools", FAUSSE_CLE),
])
def test_masquer_urls_retire_le_secret(texte, secret):
    masque = js.masquer_urls(texte)
    assert secret not in masque
    assert "***" in masque


def test_masquer_urls_garde_le_diagnostic():
    texte = ("wss://connect.browserbase.com?apiKey=K&sessionId=sess-42 410 Gone; "
             "contact a@b.com, voir https://h.io/p?email=a@b.com")
    masque = js.masquer_urls(texte)
    assert "sessionId=sess-42" in masque and "410 Gone" in masque
    assert "a@b.com" in masque, "un @ hors autorité n'est pas une identité d'URL"
    assert js.masquer_urls("") == "" and js.masquer_urls("rien à voir") == "rien à voir"


# --- à la source : connect_cdp ------------------------------------------------ #

@pytest.mark.asyncio
async def test_connect_cdp_releve_une_erreur_sans_url(cle):
    p = _Playwright()
    url = browserbase.connect_url("sess-1")
    assert cle in url
    with pytest.raises(browserbase.BrowserbaseError) as ei:
        await browserbase.connect_cdp(p, url)
    e = ei.value
    assert cle not in str(e)
    assert "410 Gone" in str(e), "le diagnostic amont reste lisible"
    assert e.__cause__ is None and e.__suppress_context__, \
        "la cause brute (qui porte l'URL) ne voyage pas avec l'exception"


@pytest.mark.asyncio
async def test_connect_cdp_masque_la_cle_nue(cle):
    """Une erreur qui citerait la clé HORS d'une URL reconnue est masquée aussi."""
    p = _Playwright(message=f"handshake rejected for key {cle}")
    with pytest.raises(browserbase.BrowserbaseError) as ei:
        await browserbase.connect_cdp(p, browserbase.connect_url("sess-1"))
    assert cle not in str(ei.value)


@pytest.mark.asyncio
async def test_run_page_eval_ne_fuit_pas_la_signing_key(cle, monkeypatch):
    """Le chemin des outils (`browser_eval`, `pennylaneged`, `brevoauto`…) : la
    `connectUrl` de session porte `signingKey`."""
    import patchright.async_api as pw

    p = _Playwright()
    connect_url = f"wss://connect.browserbase.com?signingKey={FAUX_SIGNING}"
    monkeypatch.setattr(pw, "async_playwright", lambda: p)
    monkeypatch.setattr(browserbase, "start_session",
                        lambda _ctx: {"id": "sess-2", "connectUrl": connect_url})
    monkeypatch.setattr(browserbase, "release_session", lambda _sid: None)

    with pytest.raises(browserbase.BrowserbaseError) as ei:
        await browserbase.run_page_eval("ctx-1", "https://app.example/", "() => 1")
    assert p.chromium.urls == [connect_url], "on s'est bien branché sur la connectUrl"
    assert FAUX_SIGNING not in str(ei.value)
    assert cle not in str(ei.value)


@pytest.mark.asyncio
async def test_navigate_puis_journal_la_cle_n_atteint_pas_tool_calls(cle, monkeypatch):
    """Bout en bout : l'échec de branchement, tel qu'un outil le laisserait
    remonter dans `tool_calls.error`, n'y porte pas la clé."""
    import patchright.async_api as pw
    from oto_mcp.db import usage

    monkeypatch.setattr(pw, "async_playwright", lambda: _Playwright())
    with pytest.raises(browserbase.BrowserbaseError) as ei:
        await browserbase.navigate("sess-3", "https://app.example/login")

    captured = _capturer_insert(monkeypatch)
    usage.insert_tool_call({"tool": "browser_connect_start", "ok": False,
                            "error": f"Browserbase execution failed: {ei.value}"})
    assert all(cle not in str(v) for v in captured["params"])


# --- en profondeur : insert_tool_call ---------------------------------------- #

def test_insert_tool_call_masque_une_erreur_brute(monkeypatch):
    """Défense en profondeur : même si un chemin futur laisse passer le message
    brut de Playwright, la ligne écrite ne porte ni la clé ni la signing key."""
    from oto_mcp.db import usage

    captured = _capturer_insert(monkeypatch)
    brut = (_erreur_playwright(
        f"wss://connect.browserbase.com?apiKey={FAUSSE_CLE}&sessionId=s9")
        + f" / wss://connect.browserbase.com?signingKey={FAUX_SIGNING}"
        + f" / https://user:{FAUSSE_CLE}@proxy.example:8080/")
    usage.insert_tool_call({"tool": "pennylaneged_tree", "ok": False, "error": brut})

    params = captured["params"]
    assert all(FAUSSE_CLE not in str(v) and FAUX_SIGNING not in str(v) for v in params)
    stocke = next(v for v in params if isinstance(v, str) and "connect_over_cdp" in v)
    assert "sessionId=s9" in stocke and "410 Gone" in stocke
    assert captured["sql"].count("%s") == len(params)


def test_insert_tool_call_sans_erreur_inchange(monkeypatch):
    from oto_mcp.db import usage

    captured = _capturer_insert(monkeypatch)
    usage.insert_tool_call({"tool": "fr_get", "ok": True, "error": None})
    assert captured["params"][7] is None


# --- Sentry ------------------------------------------------------------------ #

def test_sentry_masque_l_url_du_message_d_exception():
    from oto_mcp import sentry_setup

    event = {"exception": {"values": [{
        "type": "Error",
        "value": _erreur_playwright(
            f"wss://connect.browserbase.com?apiKey={FAUSSE_CLE}&sessionId=s1")}]},
        "logentry": {"message": f"boom wss://h?signingKey={FAUX_SIGNING}"}}
    out = sentry_setup._before_send(event, {})
    assert out is not None
    assert FAUSSE_CLE not in str(out) and FAUX_SIGNING not in str(out)


def test_masquer_urls_reste_lineaire_sur_un_long_texte():
    # Le schéma ne démarre qu'en tête d'une suite `[A-Za-z0-9+.-]` : sinon chaque
    # `\b` intérieur relance une tentative, et ce texte coûtait des secondes.
    import time
    debut = time.perf_counter()
    js.masquer_urls("a-" * 20000)
    assert time.perf_counter() - debut < 0.5
    assert js.masquer_urls("x https://u:p@h.example/ y") == "x https://***@h.example/ y"
