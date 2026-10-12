"""La limite de débit applicative (F-025) : `oto_mcp/limite_debit.py`.

Chaque test décrit ce que voit un client sur le fil — le statut, `Retry-After`, le
corps — pour une suite de requêtes ASGI réelles, horloge injectée. Les adresses sont
celles des plages de documentation (RFC 5737)."""
from __future__ import annotations

import json
import logging

import pytest

from oto_mcp import limite_debit as ld

A, B = "192.0.2.10", "198.51.100.20"


class Horloge:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _app_qui_rend(statut: int = 200, morceaux: tuple[bytes, ...] = (b"ok",)):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": statut, "headers": []})
        for i, m in enumerate(morceaux):
            await send({"type": "http.response.body", "body": m,
                        "more_body": i < len(morceaux) - 1})
    return app


def _scope(chemin="/mcp", methode="POST", ip=A, jeton=None, entetes=()):
    h = [(b"cf-connecting-ip", ip.encode())]
    if jeton is not None:
        h.append((b"authorization", f"Bearer {jeton}".encode()))
    h += list(entetes)
    return {"type": "http", "method": methode, "path": chemin, "headers": h,
            "client": ("127.0.0.1", 5000)}


async def _appel(garde, scope):
    msgs = []

    async def receive():                                   # pragma: no cover
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        msgs.append(m)

    await garde(scope, receive, send)
    return msgs


def _statut(msgs):
    return msgs[0]["status"]


def _entetes(msgs):
    return {k: v for k, v in msgs[0]["headers"]}


def _garde(app=None, mode=ld.ENFORCE, horloge=None, **par_minute):
    bornes = {c: ld.Borne(par_minute.get(c, d)) for c, d in ld.DEFAUTS.items()}
    return ld.LimiteDebit(app or _app_qui_rend(), mode=mode, bornes=bornes,
                          horloge=horloge or Horloge())


# ── la classe d'une requête ────────────────────────────────────────────────

@pytest.mark.parametrize("methode,chemin,jeton,classe", [
    ("OPTIONS", "/api/me", False, None),
    ("POST", "/mcp", True, "jeton"),
    ("GET", "/api/me/projects", True, "jeton"),
    ("POST", "/mcp", False, "anonyme"),
    ("GET", "/.well-known/oauth-protected-resource", False, "anonyme"),
    ("POST", "/api/upload/abc", False, "anonyme"),
    ("GET", "/api/invitations/abc", False, "sensible"),
    ("GET", "/api/public/docs/abc", False, "sensible"),
    ("GET", "/p/d/abc", False, "sensible"),
    ("POST", "/o/u/abc", False, "sensible"),
    ("POST", "/oauth/register", False, "sensible"),
    ("GET", "/oauth/authorize", False, "sensible"),
    ("GET", "/api/zoho/oauth/callback", False, "sensible"),
    ("POST", "/api/hooks/42", False, "webhook"),
    ("POST", "/api/hooks/42", True, "webhook"),
    ("POST", "/api/receivers/apollo/phones/abc", False, "webhook"),
    ("POST", "/api/billing/webhook", False, "webhook"),
])
def test_classer(methode, chemin, jeton, classe):
    assert ld.classer(methode, chemin, jeton) == classe


# ── les seaux ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_un_jeton_qui_martele_recoit_429_avec_retry_after_puis_repasse():
    h = Horloge()
    garde = _garde(horloge=h, jeton=60)              # rafale 30, 1 jeton/s
    for _ in range(30):
        assert _statut(await _appel(garde, _scope(jeton="t1"))) == 200
    msgs = await _appel(garde, _scope(jeton="t1"))
    assert _statut(msgs) == 429
    entetes = _entetes(msgs)
    assert entetes[b"retry-after"] == b"1"
    assert b"x-oto-version" in entetes
    corps = json.loads(msgs[1]["body"])
    assert corps["error"] == "rate_limited"
    assert corps["details"] == {"retryable": True, "retry_after_seconds": 1,
                                "limit_per_minute": 60, "scope": "jeton"}
    h.t += 1.0
    assert _statut(await _appel(garde, _scope(jeton="t1"))) == 200


@pytest.mark.asyncio
async def test_le_jeton_est_la_cle_pas_ladresse():
    """Un client hébergé sert tous ses utilisateurs depuis quelques adresses : un jeton
    épuisé ne coupe pas son voisin de même adresse, et changer d'adresse ne remplit
    pas le seau d'un jeton."""
    garde = _garde(jeton=2)                          # rafale 1
    assert _statut(await _appel(garde, _scope(jeton="t1", ip=A))) == 200
    assert _statut(await _appel(garde, _scope(jeton="t1", ip=B))) == 429
    assert _statut(await _appel(garde, _scope(jeton="t2", ip=A))) == 200


@pytest.mark.asyncio
async def test_lanonyme_est_borne_par_adresse():
    garde = _garde(anonyme=2)
    assert _statut(await _appel(garde, _scope(ip=A))) == 200
    assert _statut(await _appel(garde, _scope(ip=A))) == 429
    assert _statut(await _appel(garde, _scope(ip=B))) == 200


@pytest.mark.asyncio
async def test_ladresse_suit_la_regle_unique_du_depot():
    """`CF-Connecting-IP` d'abord (`client_trace.pick_ip`), jamais le pair TCP du
    proxy : sans quoi tout le trafic partagerait le seau de `127.0.0.1`."""
    garde = _garde(anonyme=2)
    s = _scope(ip=A)
    assert s["client"][0] == "127.0.0.1"
    assert _statut(await _appel(garde, s)) == 200
    assert _statut(await _appel(garde, _scope(ip=B))) == 200


@pytest.mark.asyncio
async def test_les_routes_sensibles_sont_plus_strictes_que_lanonyme():
    garde = _garde(sensible=2, anonyme=600)
    assert _statut(await _appel(garde, _scope("/api/invitations/x", "GET"))) == 200
    msgs = await _appel(garde, _scope("/api/invitations/y", "GET"))
    assert _statut(msgs) == 429
    assert json.loads(msgs[1]["body"])["details"]["scope"] == "sensible"
    assert _statut(await _appel(garde, _scope("/mcp"))) == 200


@pytest.mark.asyncio
async def test_un_webhook_se_compte_par_adresse_et_par_crochet():
    garde = _garde(webhook=2)
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=A))) == 200
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=A))) == 429
    assert _statut(await _appel(garde, _scope("/api/hooks/2", ip=A))) == 200
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=B))) == 200


@pytest.mark.asyncio
async def test_options_passe_toujours():
    garde = _garde(anonyme=2)
    for _ in range(10):
        assert _statut(await _appel(garde, _scope("/api/me", "OPTIONS"))) == 200


@pytest.mark.asyncio
async def test_zero_desarme_une_classe():
    garde = _garde(anonyme=0)
    for _ in range(50):
        assert _statut(await _appel(garde, _scope())) == 200


# ── le budget d'échecs : le jeton inventé ne s'achète pas un seau neuf ─────

@pytest.mark.asyncio
async def test_des_jetons_inventes_epuisent_le_budget_de_ladresse():
    garde = _garde(app=_app_qui_rend(401), echecs=4)    # rafale 2
    for i in range(2):
        assert _statut(await _appel(garde, _scope(jeton=f"faux{i}"))) == 401
    msgs = await _appel(garde, _scope(jeton="faux-suivant"))
    assert _statut(msgs) == 429
    assert json.loads(msgs[1]["body"])["details"]["scope"] == "echecs"
    # une autre adresse n'en pâtit pas
    assert _statut(await _appel(garde, _scope(jeton="faux9", ip=B))) == 401


@pytest.mark.asyncio
async def test_un_jeton_deja_accepte_passe_malgre_le_budget_epuise():
    """Les 401 de découverte d'un client hébergé remplissent le compteur de son
    adresse ; ses utilisateurs déjà connectés ne doivent pas en pâtir."""
    statuts = {"bon": 200}

    async def app(scope, receive, send):
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        statut = statuts.get(auth.removeprefix("Bearer "), 401)
        await send({"type": "http.response.start", "status": statut, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    garde = _garde(app=app, echecs=4)
    assert _statut(await _appel(garde, _scope(jeton="bon"))) == 200
    for i in range(2):
        await _appel(garde, _scope(jeton=f"faux{i}"))
    assert _statut(await _appel(garde, _scope(jeton="faux-suivant"))) == 429
    assert _statut(await _appel(garde, _scope(jeton="bon"))) == 200
    # la découverte OAuth sans jeton (classe `anonyme`) n'est jamais fermée par les
    # 401 des voisins : elle paie son propre seau par adresse.
    assert _statut(await _appel(garde, _scope())) == 401


@pytest.mark.asyncio
async def test_un_404_compte_comme_echec_sur_une_route_a_secret_seulement():
    garde = _garde(app=_app_qui_rend(404), echecs=4, sensible=600, jeton=600)
    for _ in range(5):
        await _appel(garde, _scope("/api/me/x", "GET", jeton="t"))
    assert _statut(await _appel(garde, _scope("/api/me/x", "GET", jeton="u"))) == 404
    for _ in range(2):
        await _appel(garde, _scope("/api/invitations/devine", "GET"))
    assert _statut(await _appel(garde, _scope("/api/invitations/autre", "GET"))) == 429


# ── modes, transport ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_laisse_passer_et_le_dit(caplog):
    garde = _garde(mode=ld.REPORT, anonyme=2)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_debit"):
        for _ in range(5):
            assert _statut(await _appel(garde, _scope())) == 200
    lignes = [r.getMessage() for r in caplog.records]
    assert len(lignes) == 1, "une ligne par clé et par minute, pas une par requête"
    assert "serait refusé" in lignes[0] and A in lignes[0]


@pytest.mark.asyncio
async def test_le_journal_ne_porte_jamais_le_jeton(caplog):
    garde = _garde(jeton=2)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_debit"):
        for _ in range(3):
            await _appel(garde, _scope(jeton="secret-tres-long"))
    assert caplog.records
    assert all("secret-tres-long" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_off_est_un_passe_partout():
    garde = _garde(mode=ld.OFF, anonyme=2)
    for _ in range(5):
        assert _statut(await _appel(garde, _scope())) == 200


@pytest.mark.asyncio
async def test_le_flux_passe_intact_sans_tampon():
    """Le SSE de `/mcp` : chaque morceau ressort tel quel, dans l'ordre."""
    garde = _garde(app=_app_qui_rend(200, (b"a", b"b", b"c")))
    msgs = await _appel(garde, _scope(jeton="t"))
    assert [m.get("body") for m in msgs[1:]] == [b"a", b"b", b"c"]
    assert [m.get("more_body") for m in msgs[1:]] == [True, True, False]


@pytest.mark.asyncio
async def test_hors_http_rien_nest_touche():
    vus = []

    async def app(scope, receive, send):
        vus.append(scope["type"])

    garde = _garde(app=app, anonyme=2)
    for _ in range(5):
        await garde({"type": "lifespan"}, None, None)
    assert vus == ["lifespan"] * 5


# ── réglages ───────────────────────────────────────────────────────────────

def test_mode_inconnu_vaut_enforce(monkeypatch):
    monkeypatch.setenv("OTO_DEBIT_MODE", "enforced")
    assert ld.mode_depuis_env() == ld.ENFORCE
    monkeypatch.setenv("OTO_DEBIT_MODE", "Report")
    assert ld.mode_depuis_env() == ld.REPORT
    monkeypatch.delenv("OTO_DEBIT_MODE")
    assert ld.mode_depuis_env() == ld.ENFORCE


def test_bornes_lues_dans_lenv(monkeypatch):
    monkeypatch.setenv("OTO_DEBIT_JETON_PAR_MIN", "30")
    monkeypatch.setenv("OTO_DEBIT_ANONYME_PAR_MIN", "beaucoup")
    monkeypatch.setenv("OTO_DEBIT_SENSIBLE_PAR_MIN", "0")
    monkeypatch.delenv("OTO_DEBIT_WEBHOOK_PAR_MIN", raising=False)
    b = ld.bornes_depuis_env()
    assert b["jeton"].par_minute == 30 and b["jeton"].rafale == 15
    assert b["anonyme"].par_minute == ld.DEFAUTS["anonyme"]
    assert b["sensible"].par_minute == 0
    assert b["webhook"].par_minute == ld.DEFAUTS["webhook"]


def test_le_defaut_par_jeton_ne_contredit_aucune_route_lourde():
    """Le débit par jeton s'applique EN PLUS des routes lourdes : réglé plus bas que
    l'une d'elles, il couperait le relevé légitime qu'elle a été réglée pour laisser
    passer (#1145)."""
    from oto_mcp.api.routes_lourdes import ROUTES_LOURDES
    assert ld.DEFAUTS["jeton"] >= max(r.par_minute for r in ROUTES_LOURDES)
