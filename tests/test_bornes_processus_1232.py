"""Les bornes globales du processus (#1232, lot 4) : `oto_mcp/bornes_processus.py`.

Ce qu'on éprouve, sans base : chaque borne (corps, délai d'outil, `statement_timeout`,
inactivité des sessions MCP) MESURE en `report` (défaut) sans rien refuser, APPLIQUE en
`enforce`, se tait en `off` — sous l'interrupteur de la limite de débit (`OTO_DEBIT_MODE`),
pas un second ; un dépassement va au palmarès des refus sous une clé HACHÉE ; un réglage
illisible lève. Adresses des plages de documentation (RFC 5737)."""
from __future__ import annotations

import asyncio
import json
import re
import types

import psycopg
import pytest

from oto_mcp import bornes_processus as bp
from oto_mcp import limite_refus_top as top

A = "192.0.2.10"
T0 = 1_760_000_000.0


def _reglages(mode=bp.REPORT, **kw):
    base = dict(mode=mode, corps_octets=100, corps_upload_octets=1000, outil_s=120,
                sql_ms=30_000, session_inactive_s=1800)
    base.update(kw)
    return bp.Reglages(**base)


@pytest.fixture
def palmares(monkeypatch):
    """Des palmarès neufs à la place de ceux du processus, publications captées ;
    les réglages relus de l'environnement à chaque banc."""
    neufs = {lim: top.Palmares(lim) for lim in top.PALMARES_BORNES}
    publiees: list = []
    monkeypatch.setattr(top, "PALMARES_BORNES", neufs)
    monkeypatch.setattr(top, "publier", publiees.extend)
    monkeypatch.setattr(bp, "_journalise", {})
    lus = bp.reglages                     # un banc peut le remplacer : on garde le vrai
    lus.cache_clear()
    yield neufs
    lus.cache_clear()


def _lignes(palmares, limiteur):
    return palmares[limiteur].basculer(10 ** 10)


# ── les réglages ───────────────────────────────────────────────────────────────

def test_les_defauts_sont_ceux_decides_et_le_mode_est_celui_du_debit(monkeypatch):
    for nom in ("OTO_DEBIT_MODE", "OTO_BORNE_CORPS_OCTETS", "OTO_BORNE_CORPS_UPLOAD_OCTETS",
                "OTO_BORNE_OUTIL_S", "OTO_MCP_DB_STATEMENT_TIMEOUT_MS",
                "OTO_BORNE_SESSION_INACTIVE_S"):
        monkeypatch.delenv(nom, raising=False)
    r = bp.lire()
    assert r == bp.Reglages(mode="report", corps_octets=4 * 1024 * 1024,
                            corps_upload_octets=110 * 1024 * 1024, outil_s=120,
                            sql_ms=30_000, session_inactive_s=1800)
    assert not r.applique and r.actif
    monkeypatch.setenv("OTO_DEBIT_MODE", "enforce")
    monkeypatch.setenv("OTO_BORNE_OUTIL_S", "0")
    r = bp.lire()
    assert r.applique and r.outil_s == 0, "`0` désarme une borne"


@pytest.mark.parametrize("nom", ["OTO_BORNE_CORPS_OCTETS", "OTO_BORNE_CORPS_UPLOAD_OCTETS",
                                 "OTO_BORNE_OUTIL_S", "OTO_MCP_DB_STATEMENT_TIMEOUT_MS",
                                 "OTO_BORNE_SESSION_INACTIVE_S"])
@pytest.mark.parametrize("brut", ["4Mo", "-1", "1.5"])
def test_un_reglage_illisible_leve_au_demarrage(monkeypatch, nom, brut):
    monkeypatch.setenv(nom, brut)
    bp.reglages.cache_clear()
    try:
        with pytest.raises(bp.ReglageBorneInvalide, match=nom):
            bp.verifier_configuration()
    finally:
        bp.reglages.cache_clear()


def test_un_mode_illisible_leve_comme_pour_le_debit(monkeypatch):
    from oto_mcp.limite_debit import ReglageDebitInvalide
    monkeypatch.setenv("OTO_DEBIT_MODE", "enforced")
    with pytest.raises(ReglageDebitInvalide):
        bp.lire()


def test_le_main_verifie_les_bornes_au_demarrage():
    import inspect
    from oto_mcp import server
    assert "bornes_processus.verifier_configuration()" in inspect.getsource(server.main)


# ── la taille du corps ────────────────────────────────────────────────────────

def _scope(chemin="/mcp", methode="POST", longueur=None, jeton=None):
    h = [(b"host", b"oto.example")]
    if longueur is not None:
        h.append((b"content-length", str(longueur).encode()))
    if jeton is not None:
        h.append((b"authorization", f"Bearer {jeton}".encode()))
    return {"type": "http", "method": methode, "path": chemin, "headers": h,
            "client": (A, 5000)}


def _recepteur(morceaux):
    file = list(morceaux)

    async def receive():
        if not file:
            return {"type": "http.disconnect"}
        corps = file.pop(0)
        return {"type": "http.request", "body": corps, "more_body": bool(file)}
    return receive


async def _servir(garde, scope, morceaux=(b"",)):
    envoyes: list = []

    async def send(m):
        envoyes.append(m)

    await garde(scope, _recepteur(morceaux), send)
    statut = next(m["status"] for m in envoyes if m["type"] == "http.response.start")
    corps = b"".join(m.get("body", b"") for m in envoyes if m["type"] == "http.response.body")
    return statut, corps, envoyes


def _app_qui_lit():
    vu = {"octets": 0, "appelee": False}

    async def app(scope, receive, send):
        vu["appelee"] = True
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                raise RuntimeError("client parti")          # comme Starlette
            vu["octets"] += len(m.get("body") or b"")
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app, vu


class _Route:
    """Une route vivante : son motif et son handler, qui porte (ou non) un contrat."""

    def __init__(self, chemin, contrat=None):
        from starlette.routing import compile_path
        self.path_regex = compile_path(chemin)[0]

        async def endpoint(request):                      # pragma: no cover
            return None
        if contrat is not None:
            endpoint.contrat = contrat
        self.endpoint = endpoint


def _routes():
    from oto_mcp.capabilities._types import ContratDeRoute
    return [
        _Route("/api/depot/{x}", ContratDeRoute(description="d", corps={
            "PUT": {"application/octet-stream": {"type": "string"}},
            "POST": {"application/json": {"type": "object"}}})),
        _Route("/api/json", ContratDeRoute(description="j", corps={
            "POST": {"application/json": {"type": "object"}}})),
        _Route("/api/rien"),
    ]


def test_une_route_d_upload_se_reconnait_a_son_contrat_jamais_a_son_chemin():
    from oto_mcp.borne_corps import routes_upload
    (motif, verbes), = routes_upload(_routes())
    assert verbes == {"PUT"}, "le POST JSON du même chemin n'est pas un upload"
    assert motif.match("/api/depot/abc")


def test_les_routes_d_upload_servies_sont_reconnues():
    """Sur la table de routes REST réelle : chaque dépôt de fichier, et aucune route
    JSON. Un dépôt sans contrat serait borné à 4 Mo en application."""
    from oto_mcp.borne_corps import BorneCorps, UPLOAD, DEFAUT
    from oto_mcp.server import routes_rest
    garde = BorneCorps(None, routes=routes_rest(None), reglages=_reglages())
    for methode, chemin in [("PUT", "/api/upload/jeton"), ("POST", "/api/upload/jeton"),
                            ("POST", "/api/me/projects/7/files"),
                            ("POST", "/api/me/projects/7/transcriptions/upload"),
                            ("POST", "/api/me/avatar"), ("POST", "/api/orgs/3/logo"),
                            ("POST", "/api/relay/jeton")]:
        assert garde._classe(methode, chemin) == UPLOAD, (methode, chemin)
    for methode, chemin in [("POST", "/api/me/projects"), ("GET", "/api/upload/jeton"),
                            ("POST", "/api/me/datastores"), ("POST", "/mcp")]:
        assert garde._classe(methode, chemin) == DEFAUT, (methode, chemin)


@pytest.mark.asyncio
async def test_en_mesure_un_corps_trop_gros_passe_et_se_compte(palmares):
    from oto_mcp.borne_corps import BorneCorps
    app, vu = _app_qui_lit()
    garde = BorneCorps(app, routes=_routes(), reglages=_reglages())
    statut, _, _ = await _servir(garde, _scope(longueur=500), [b"x" * 500])
    assert (statut, vu["octets"]) == (200, 500), "report : rien ne change"
    statut, _, _ = await _servir(garde, _scope(jeton="secret-jeton"), [b"x" * 60] * 3)
    assert statut == 200
    statut, _, _ = await _servir(garde, _scope("/api/depot/z", "PUT", longueur=900),
                                 [b"x" * 900])
    assert statut == 200, "sous la borne d'upload"
    lignes = _lignes(palmares, "corps")
    assert sorted((l["classe"], l["refus"], l["mode"]) for l in lignes) == [
        ("defaut", 1, "report"), ("defaut", 1, "report")]
    cles = {l["cle"] for l in lignes}
    assert cles == {top.empreinte(A), top.empreinte("Bearer secret-jeton")}
    assert "secret-jeton" not in json.dumps(lignes, default=str)


@pytest.mark.asyncio
async def test_en_application_un_corps_annonce_trop_gros_part_en_413_sans_rien_lire(
        palmares):
    from oto_mcp.borne_corps import BorneCorps
    app, vu = _app_qui_lit()
    garde = BorneCorps(app, reglages=_reglages(bp.ENFORCE))
    statut, corps, envoyes = await _servir(
        garde, _scope(longueur=101) | {"headers": _scope(longueur=101)["headers"]
                                       + [(b"origin", b"https://app.example")]})
    assert statut == 413 and not vu["appelee"]
    assert json.loads(corps)["error"] == "payload_too_large"
    entetes = dict(envoyes[0]["headers"])
    assert b"x-oto-version" in entetes
    assert _lignes(palmares, "corps")[0]["mode"] == "enforce"


@pytest.mark.asyncio
async def test_en_application_un_corps_en_flux_est_coupe_et_le_413_remplace_la_reponse(
        palmares):
    from oto_mcp.borne_corps import BorneCorps
    app, vu = _app_qui_lit()
    garde = BorneCorps(app, reglages=_reglages(bp.ENFORCE))
    statut, corps, envoyes = await _servir(garde, _scope(), [b"x" * 60] * 5)
    assert statut == 413
    assert vu["octets"] == 60, "la lecture s'arrête au premier morceau de trop"
    assert [m["type"] for m in envoyes] == ["http.response.start", "http.response.body"]
    assert json.loads(corps)["details"]["limit_bytes"] == 100


@pytest.mark.asyncio
async def test_en_application_un_upload_a_sa_borne(palmares):
    from oto_mcp.borne_corps import BorneCorps
    app, _ = _app_qui_lit()
    garde = BorneCorps(app, routes=_routes(), reglages=_reglages(bp.ENFORCE))
    assert (await _servir(garde, _scope("/api/depot/z", "PUT", longueur=900),
                          [b"x" * 900]))[0] == 200
    assert (await _servir(garde, _scope("/api/depot/z", "PUT", longueur=1001)))[0] == 413


@pytest.mark.asyncio
async def test_off_ne_mesure_rien(palmares):
    from oto_mcp.borne_corps import BorneCorps
    app, _ = _app_qui_lit()
    garde = BorneCorps(app, reglages=_reglages(bp.OFF))
    assert (await _servir(garde, _scope(longueur=500), [b"x" * 500]))[0] == 200
    assert _lignes(palmares, "corps") == []


# ── le délai d'un appel d'outil ───────────────────────────────────────────────

def _contexte(nom="data_rows"):
    return types.SimpleNamespace(message=types.SimpleNamespace(name=nom, arguments={}))


@pytest.mark.asyncio
async def test_en_mesure_un_appel_trop_long_aboutit_et_se_compte(palmares):
    from oto_mcp.middleware.delai_outil import DelaiOutilMiddleware
    temps = iter([0.0, 130.0, 0.0, 10.0])
    mw = DelaiOutilMiddleware(reglages=_reglages(), horloge=lambda: next(temps))

    async def suite(_ctx):
        return "résultat"

    assert await mw.on_call_tool(_contexte("data_rows"), suite) == "résultat"
    assert await mw.on_call_tool(_contexte("fr_get"), suite) == "résultat"
    (ligne,) = _lignes(palmares, "outil")
    assert (ligne["classe"], ligne["refus"], ligne["mode"], ligne["cle"]) == (
        "data_rows", 1, "report", "-")


@pytest.mark.asyncio
async def test_en_application_l_appel_est_abandonne_avec_une_erreur_qui_dit_le_delai(
        palmares):
    from oto_mcp.error_taxonomy import classify
    from oto_mcp.middleware.delai_outil import DelaiOutilMiddleware
    mw = DelaiOutilMiddleware(reglages=_reglages(bp.ENFORCE, outil_s=0.05))
    fini = {"ok": False}

    async def lent(_ctx):
        await asyncio.sleep(5)
        fini["ok"] = True

    with pytest.raises(bp.DelaiOutilDepasse) as e:
        await mw.on_call_tool(_contexte(), lent)
    assert not fini["ok"]
    info = classify(e.value)
    assert info.code == "tool_timeout" and "0.05 s" in info.message
    assert not info.retryable
    assert _lignes(palmares, "outil")[0]["mode"] == "enforce"
    tache = asyncio.current_task()
    assert not getattr(tache, "cancelling", lambda: 0)(), "l'annulation est consommée"


@pytest.mark.asyncio
async def test_en_application_un_appel_rapide_et_ses_erreurs_passent_intacts(palmares):
    from oto_mcp.middleware.delai_outil import DelaiOutilMiddleware
    mw = DelaiOutilMiddleware(reglages=_reglages(bp.ENFORCE, outil_s=5))

    async def vite(_ctx):
        return 42

    async def delai_amont(_ctx):
        raise TimeoutError("amont")

    assert await mw.on_call_tool(_contexte(), vite) == 42
    with pytest.raises(TimeoutError, match="amont") as e:
        await mw.on_call_tool(_contexte(), delai_amont)
    assert not isinstance(e.value, bp.DelaiOutilDepasse)
    await asyncio.sleep(0)
    assert _lignes(palmares, "outil") == []


@pytest.mark.asyncio
async def test_une_annulation_d_ailleurs_n_est_pas_un_depassement(palmares):
    from oto_mcp.middleware.delai_outil import DelaiOutilMiddleware
    mw = DelaiOutilMiddleware(reglages=_reglages(bp.ENFORCE, outil_s=5))

    async def annule(_ctx):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mw.on_call_tool(_contexte(), annule)
    assert _lignes(palmares, "outil") == []


# ── statement_timeout ────────────────────────────────────────────────────────

def test_statement_timeout_pose_sur_le_pool_en_application_seulement(monkeypatch):
    from oto_mcp.db import _conn
    monkeypatch.setattr(bp, "reglages", lambda: _reglages())
    assert "statement_timeout" not in _conn._connect_options()
    monkeypatch.setattr(bp, "reglages", lambda: _reglages(bp.ENFORCE))
    assert "-c statement_timeout=30000" in _conn._connect_options()
    assert "statement_timeout" not in _conn._connect_options(borne_par_defaut=False)
    ddl = _conn._ddl_options()
    assert re.findall(r"statement_timeout=(\d+)", ddl) == ["60000"], \
        "le DDL à chaud garde SA borne, pas celle du pool"
    monkeypatch.setattr(bp, "reglages", lambda: _reglages(bp.ENFORCE, sql_ms=0))
    assert "statement_timeout" not in _conn._connect_options()


def test_le_pool_mesure_par_son_curseur(monkeypatch):
    from oto_mcp.db import _conn
    from oto_mcp.db.borne_sql import CurseurBorne
    vus = {}

    class _Pool:
        def __init__(self, **kw):
            vus.update(kw)

    monkeypatch.setattr(_conn, "ConnectionPool", _Pool)
    monkeypatch.setattr(_conn, "_pool", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://banc:1/rien")
    _conn._get_pool()
    assert vus["kwargs"]["cursor_factory"] is CurseurBorne


def test_la_preparation_de_la_base_libere_sa_transaction_de_la_borne():
    import inspect
    from oto_mcp.db import _init
    assert "set_config('statement_timeout', '0', true)" in inspect.getsource(
        _init.apply_boot_schema)


class _Conn:
    def __init__(self, propre_ms):
        self.propre_ms, self.lectures = propre_ms, 0

    def cursor(self, row_factory=None):
        conn = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q):
                conn.lectures += 1

            def fetchone(self):
                return (conn.propre_ms,)
        return _C()


def _curseur(propre_ms=0):
    return types.SimpleNamespace(connection=_Conn(propre_ms))


def _lancer_ordre(curseur, duree, erreur=None):
    from oto_mcp.db.borne_sql import constater
    constater(curseur, "SELECT * FROM t WHERE id = %s", duree, erreur)


def test_en_mesure_un_ordre_trop_long_sans_borne_propre_se_compte(palmares, monkeypatch):
    monkeypatch.setattr(bp, "reglages", lambda: _reglages())
    rapide, long_, borne = _curseur(), _curseur(0), _curseur(60_000)
    _lancer_ordre(rapide, 1.0)
    _lancer_ordre(long_, 31.0)
    _lancer_ordre(borne, 31.0)
    assert rapide.connection.lectures == 0, "sous la borne : rien, pas même une lecture"
    (ligne,) = _lignes(palmares, "sql")
    assert ligne["classe"] == f"{__name__}:_lancer_ordre"
    assert ligne["cle"] == top.empreinte("SELECT * FROM t WHERE id = %s")
    assert ligne["mode"] == "report"


def test_en_mesure_une_annulation_est_celle_du_chemin(palmares, monkeypatch):
    monkeypatch.setattr(bp, "reglages", lambda: _reglages())
    _lancer_ordre(_curseur(), 45.0, psycopg.errors.QueryCanceled())
    assert _lignes(palmares, "sql") == []


def test_en_application_une_annulation_au_dela_de_la_borne_se_compte(palmares, monkeypatch):
    monkeypatch.setattr(bp, "reglages", lambda: _reglages(bp.ENFORCE))
    curseur = _curseur()
    _lancer_ordre(curseur, 30.0, psycopg.errors.QueryCanceled())
    _lancer_ordre(curseur, 40.0)                # passé : le chemin a sa propre borne
    _lancer_ordre(curseur, 31.0, RuntimeError("autre"))
    (ligne,) = _lignes(palmares, "sql")
    assert (ligne["refus"], ligne["mode"]) == (1, "enforce")
    assert curseur.connection.lectures == 0


# ── l'inactivité des sessions MCP ────────────────────────────────────────────

class _Horloge:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _app_mcp(nouvelle=b"s-neuve"):
    async def app(scope, receive, send):
        entetes = [] if any(k == b"mcp-session-id" for k, _ in scope["headers"]) \
            else [(b"mcp-session-id", nouvelle)]
        await send({"type": "http.response.start", "status": 200, "headers": entetes})
        await send({"type": "http.response.body", "body": b""})
    return app


def _scope_mcp(session=None, methode="POST"):
    s = _scope(methode=methode)
    if session is not None:
        s["headers"] = s["headers"] + [(b"mcp-session-id", session)]
    return s


@pytest.mark.asyncio
async def test_en_mesure_une_session_reprise_apres_la_borne_se_compte(palmares):
    from oto_mcp.borne_session import BorneSession
    horloge = _Horloge()
    garde = BorneSession(_app_mcp(), reglages=_reglages(session_inactive_s=1800),
                         horloge=horloge)
    await _servir(garde, _scope_mcp())                    # initialize : la session naît
    horloge.t = 1000
    await _servir(garde, _scope_mcp(b"s-neuve"))          # active
    horloge.t = 1000 + 1801
    statut, _, _ = await _servir(garde, _scope_mcp(b"s-neuve"))
    assert statut == 200, "report : la session sert toujours"
    await _servir(garde, _scope_mcp(b"inconnue"))         # jamais vue ici : rien
    (ligne,) = _lignes(palmares, "session")
    assert (ligne["classe"], ligne["refus"], ligne["cle"]) == ("mcp", 1, top.empreinte(A))


@pytest.mark.asyncio
async def test_un_delete_oublie_la_session(palmares):
    from oto_mcp.borne_session import BorneSession
    horloge = _Horloge()
    garde = BorneSession(_app_mcp(), reglages=_reglages(), horloge=horloge)
    await _servir(garde, _scope_mcp(b"s1"))
    await _servir(garde, _scope_mcp(b"s1", methode="DELETE"))
    horloge.t = 10_000
    await _servir(garde, _scope_mcp(b"s1"))
    assert _lignes(palmares, "session") == []


async def _demarrer(garde):
    envoyes = []
    file = [{"type": "lifespan.startup"}]

    async def receive():
        return file.pop(0) if file else {"type": "lifespan.shutdown"}

    async def send(m):
        envoyes.append(m["type"])

    await garde({"type": "lifespan"}, receive, send)
    return envoyes


def _cycle_interne():
    async def app(scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})
    return app


@pytest.mark.asyncio
async def test_en_application_le_gestionnaire_de_session_de_chaque_face_est_arme():
    from oto_mcp.borne_session import BorneSession
    faces = [types.SimpleNamespace(session_manager=types.SimpleNamespace(
        stateless=False, session_idle_timeout=None)) for _ in range(2)]
    faces.append(object())                                 # face sans gestionnaire
    garde = BorneSession(_cycle_interne(), faces=faces,
                         reglages=_reglages(bp.ENFORCE, session_inactive_s=1800))
    assert "lifespan.startup.complete" in await _demarrer(garde)
    assert [f.session_manager.session_idle_timeout for f in faces[:2]] == [1800.0, 1800.0]


@pytest.mark.asyncio
async def test_en_mesure_le_gestionnaire_n_est_pas_touche():
    from oto_mcp.borne_session import BorneSession
    face = types.SimpleNamespace(session_manager=types.SimpleNamespace(
        stateless=False, session_idle_timeout=None))
    await _demarrer(BorneSession(_cycle_interne(), faces=[face], reglages=_reglages()))
    assert face.session_manager.session_idle_timeout is None


@pytest.mark.asyncio
async def test_un_sdk_sans_la_borne_refuse_le_demarrage():
    from oto_mcp.borne_session import BorneSession, SdkSansBorneDeSession
    face = types.SimpleNamespace(session_manager=types.SimpleNamespace(stateless=False))
    garde = BorneSession(_cycle_interne(), faces=[face], reglages=_reglages(bp.ENFORCE))
    envoyes = []
    file = [{"type": "lifespan.startup"}]

    async def receive():
        return file.pop(0)

    async def send(m):
        envoyes.append(m["type"])

    with pytest.raises(SdkSansBorneDeSession):
        await garde({"type": "lifespan"}, receive, send)
    assert envoyes == ["lifespan.startup.failed"], "uvicorn doit voir l'échec"


def test_le_sdk_servi_porte_la_borne_de_session():
    """La borne s'appuie sur un réglage de l'SDK que fastmcp ne transmet pas : s'il
    disparaît à une montée de version, ce banc le dit avant le démarrage."""
    from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
    g = FastMCPStreamableHTTPSessionManager(app=None)
    assert hasattr(g, "session_idle_timeout")


# ── la minute close part avec celles de la limite de débit ───────────────────

@pytest.mark.asyncio
async def test_la_limite_de_debit_vide_aussi_les_palmares_des_bornes(palmares):
    from oto_mcp import limite_debit as ld
    murale = {"t": T0}
    refus: list = []
    bornes = {c: ld.Borne(d) for c, d in ld.DEFAUTS.items()}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    garde = ld.LimiteDebit(app, mode=ld.REPORT, bornes=bornes, exemptees=(),
                           horloge_murale=lambda: murale["t"],
                           publier=lambda lignes: None, publier_refus=refus.extend)
    await _servir(garde, _scope())
    palmares["outil"].noter("data_rows", "k", "report", T0)
    murale["t"] += 60
    await _servir(garde, _scope())
    assert [(l["limiteur"], l["classe"]) for l in refus] == [("outil", "data_rows")]
