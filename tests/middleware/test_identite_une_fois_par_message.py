"""L'identité du message : résolue UNE fois, hors boucle, et jamais celle d'un autre.

Deux choses se gardent ici, et elles ne pèsent pas le même poids.

**Le coût** — `auth.hooks.current_user_sub_from_token()` canonicalise le `sub` du jeton
par deux requêtes PG synchrones (un `SELECT sub_aliases`, un `INSERT … ON CONFLICT` sur
`users`, donc un COMMIT). Sept intermédiaires la redemandent chacun leur tour pour la
MÊME identité dans le MÊME appel : mesuré le 2026-09-09 sur la chaîne réellement
servie, **10 allers-retours PG par `tools/call`, tous dans la boucle**. `IdentityScopeMiddleware`
ouvre une portée par message et la garnit hors boucle.

**Le risque** — un cache d'identité mal borné servirait l'identité d'un utilisateur à un
autre, ce qui serait infiniment pire que le gel qu'on corrige. Les tests de la seconde
moitié de ce fichier n'ont pas d'autre objet.

La mesure ne se fait pas au chronomètre ni au source : le seam unique d'emprunt de
connexion (`db._conn._get_pool`) est remplacé par un compteur qui note chaque requête
ET le thread qui la lance. Deux crans, dont un qui MORD : la même chaîne privée de sa
portée doit repayer les dix — sans quoi un vert ici ne prouverait rien.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import types

import pytest

from oto_mcp.auth import hooks


# --------------------------------------------------------------------------- #
# Instrument : le seam d'emprunt de connexion, qui compte et note le thread
# --------------------------------------------------------------------------- #

_IDENTITE = ("FROM sub_aliases", "INSERT INTO users")


class _Curseur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []

    def __iter__(self):
        return iter([])


class _Connexion:
    def __init__(self, pool):
        self.pool = pool

    def execute(self, sql, params=None, **kw):
        texte = " ".join(str(sql).split())
        self.pool.vues.append((texte, threading.current_thread().name))
        return _Curseur(self.pool.reponse(texte))

    def cursor(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PoolCompteur:
    """Faux pool : rend des réponses canoniques (aucun alias, compte déjà là) et
    retient, pour chaque requête, son SQL et le thread qui l'a lancée."""

    def __init__(self) -> None:
        self.vues: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def connection(self):
        yield _Connexion(self)

    @staticmethod
    def reponse(sql: str):
        if "FROM sub_aliases" in sql:
            return None
        if "INSERT INTO users" in sql:
            return {"inserted": False}
        return None

    def identite(self, thread: str | None = None) -> list[tuple[str, str]]:
        return [(q, t) for (q, t) in self.vues
                if any(m in q for m in _IDENTITE) and (thread is None or t == thread)]

    def remise_a_zero(self) -> None:
        self.vues.clear()


@pytest.fixture
def compteur(monkeypatch) -> _PoolCompteur:
    from oto_mcp.db import _conn

    pool = _PoolCompteur()
    monkeypatch.setattr(_conn, "_get_pool", lambda: pool)
    return pool


@pytest.fixture
def jeton(monkeypatch):
    """Un bearer MCP factice + la commande du drain armée (c'est elle qui ouvre le
    chemin des deux requêtes ; désarmée, il n'y a rien à mesurer)."""
    import fastmcp.server.dependencies as deps

    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: True)
    monkeypatch.setattr(deps, "get_access_token", lambda: types.SimpleNamespace(
        claims={"sub": "u-mesure", "email": "m@exemple.invalid", "name": "Mesure"}))


def _chaine_servie(avec_portee: bool):
    """Un banc portant la chaîne de middlewares RÉELLEMENT servie (instances comprises)
    et un outil trivial. Dérivée de l'instance de production, jamais recopiée à la
    main : une chaîne recopiée cesse un jour de ressembler à celle qui sert."""
    from fastmcp import FastMCP

    from _mcp_app import static_mcp
    from oto_mcp.middleware.identity_scope import IdentityScopeMiddleware

    banc = FastMCP("banc-identite")

    @banc.tool()
    def sonde() -> dict:
        return {"ok": True}

    for m in static_mcp().middleware:
        if isinstance(m, IdentityScopeMiddleware) and not avec_portee:
            continue
        banc.add_middleware(m)
    return banc


async def _un_appel(banc, compteur: _PoolCompteur):
    """Joue le handshake, puis DEUX appels — on ne mesure que le second : le premier
    traîne encore le `tools/list` que la visibilité de session déclenche en asynchrone,
    et mesurer un régime transitoire pour un régime permanent est un faux chiffre."""
    from fastmcp import Client

    async with Client(banc) as c:
        await c.call_tool("sonde", {})
        await asyncio.sleep(0.4)
        compteur.remise_a_zero()
        await c.call_tool("sonde", {})
        await asyncio.sleep(0.4)
    return threading.current_thread().name


# --------------------------------------------------------------------------- #
# Cran 1 — la mesure, sur la chaîne servie
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_un_tools_call_ne_resout_l_identite_qu_une_fois_et_hors_boucle(
        compteur, jeton):
    """Deux allers-retours (le SELECT et l'INSERT d'UNE résolution), zéro depuis le
    thread de la boucle."""
    boucle = await _un_appel(_chaine_servie(avec_portee=True), compteur)

    vues = compteur.identite()
    assert vues, ("garde INERTE : l'appel n'a produit AUCUNE requête d'identité — "
                  "revoir le montage (drain désarmé ? jeton absent ?) avant de "
                  "conclure au vert")
    assert len(vues) <= 2, (
        f"{len(vues)} allers-retours PG d'identité pour UN tools/call — la portée par "
        f"message ne tient plus : {[q[:48] for q, _ in vues]}")
    dans_la_boucle = compteur.identite(thread=boucle)
    assert not dans_la_boucle, (
        f"{len(dans_la_boucle)} requête(s) d'identité depuis le thread de l'event loop : "
        "le serveur est mono-loop, la canonicalisation du sub doit se payer hors boucle "
        "(cf. docs/event-loop-perf.md, mode n°5)")


@pytest.mark.asyncio
async def test_la_meme_chaine_privee_de_sa_portee_repaie_les_dix(compteur, jeton):
    """L'ÉPREUVE DE CHUTE, et elle reste dans la suite : sans `IdentityScopeMiddleware`,
    la même chaîne redemande l'identité à chaque intermédiaire — dans la boucle. Sans
    ce test, le vert du précédent pourrait tenir à un montage qui ne mesure rien."""
    boucle = await _un_appel(_chaine_servie(avec_portee=False), compteur)

    vues = compteur.identite()
    assert len(vues) >= 8, (
        f"seulement {len(vues)} allers-retours d'identité SANS la portée — l'instrument "
        "ne voit plus ce qu'il est censé attraper, donc le vert du test voisin ne "
        "prouve rien")
    assert compteur.identite(thread=boucle), (
        "sans la portée, ces requêtes partaient du thread de la boucle — si elles n'en "
        "partent plus, c'est que la mesure regarde à côté")


# --------------------------------------------------------------------------- #
# Cran 2 — ce que le cache ne doit JAMAIS faire : servir l'identité d'un autre
# --------------------------------------------------------------------------- #

@pytest.fixture
def deux_identites(monkeypatch):
    """`alice` et `bob` portent chacun un ALIAS : leur sub de jeton n'est pas leur sub
    canonique. C'est la forme qui rendrait une confusion visible — un cache qui rendrait
    la mauvaise entrée servirait `bob-canonique` à qui présente le jeton d'alice."""
    from oto_mcp import db

    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: True)
    table = {"alice-jeton": "alice-canonique", "bob-jeton": "bob-canonique"}
    vus: list[str] = []

    def resolve_sub(sub):
        vus.append(sub)
        return table.get(sub, sub)

    monkeypatch.setattr(db, "resolve_sub", resolve_sub)
    monkeypatch.setattr(db, "upsert_user", lambda sub, **kw: None)
    return types.SimpleNamespace(table=table, vus=vus)


def _porte(monkeypatch, sub: str) -> None:
    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token", lambda: types.SimpleNamespace(
        claims={"sub": sub, "email": f"{sub}@exemple.invalid", "name": sub}))


def test_deux_identites_dans_la_meme_portee_ne_partagent_aucune_entree(
        deux_identites, monkeypatch):
    """LA garde de sécurité. Dans UNE seule portée, deux jetons différents : chacun
    doit repartir avec SON identité canonique, et la seconde doit avoir été RÉSOLUE
    (pas lue dans l'entrée de la première).

    L'entrée est indexée par le sub BRUT du jeton : demander l'identité d'alice ne peut
    pas rendre celle de bob, ce serait chercher `alice-jeton` et trouver ce qui a été
    rangé sous `bob-jeton`. Ce test le vérifie plutôt que de le croire."""
    with hooks.identity_scope():
        _porte(monkeypatch, "alice-jeton")
        assert hooks.current_user_sub_from_token() == "alice-canonique"
        assert hooks.current_user_sub_from_token() == "alice-canonique"   # relu du cache

        _porte(monkeypatch, "bob-jeton")
        assert hooks.current_user_sub_from_token() == "bob-canonique", (
            "un jeton de bob a été servi sous l'identité d'alice : le cache d'identité "
            "FUIT d'un compte à l'autre — c'est la seule régression qui rende ce "
            "correctif pire que le gel qu'il corrige")
        assert hooks.current_user_sub_from_token() == "bob-canonique"

    # alice résolue une fois, bob résolu une fois : ni l'un ni l'autre n'a lu l'autre,
    # et aucun des deux n'a repayé sa propre résolution.
    assert deux_identites.vus == ["alice-jeton", "bob-jeton"], deux_identites.vus


@pytest.mark.asyncio
async def test_deux_portees_concurrentes_tiennent_chacune_leur_dictionnaire(
        deux_identites, monkeypatch):
    """Deux messages servis en même temps : chaque tâche tient SON dictionnaire, et il
    ne contient QUE sa propre clé.

    ⚠️ Ce que le test regarde est l'objet lui-même, pas seulement les valeurs rendues.
    Une première rédaction se contentait des valeurs et **restait verte** quand on
    remplaçait la portée par un dictionnaire de module — parce que la clé est le sub,
    donc chacun retrouvait quand même le sien. Le vert était juste ; il ne prouvait pas
    ce que le nom du test annonçait. Un test qui affirme une propriété qu'il ne mesure
    pas rassure, ce qui est pire qu'une absence de test.

    Le jeton est porté par une ContextVar — c'est ainsi que le vrai serveur le porte —
    au lieu d'être reposé au niveau module après chaque `await`, ce qui masquait
    justement la question posée."""
    import contextvars

    import fastmcp.server.dependencies as deps

    porteur: contextvars.ContextVar = contextvars.ContextVar("jeton_de_test", default=None)
    monkeypatch.setattr(deps, "get_access_token", lambda: porteur.get())
    vus: dict[str, dict] = {}

    async def servir(sub: str, canonique: str):
        porteur.set(types.SimpleNamespace(claims={"sub": sub, "email": None, "name": None}))
        with hooks.identity_scope():
            premier = hooks.current_user_sub_from_token()
            await asyncio.sleep(0.02)      # laisser l'autre tâche poser SA portée
            second = hooks.current_user_sub_from_token()
            dico = hooks._identity_cache.get()
            vus[sub] = dico
            assert list(dico) == [sub], (
                f"la portée de {sub} contient {list(dico)} : elle voit la clé d'une "
                "autre tâche, donc le dictionnaire est PARTAGÉ — un cache d'identité "
                "partagé entre requêtes n'est plus borné par le message")
        assert premier == second == canonique, (sub, premier, second)
        assert hooks._identity_cache.get() is None, (
            "la portée n'a pas été rendue en sortie de bloc — le dictionnaire survit à "
            "son message")

    await asyncio.gather(servir("alice-jeton", "alice-canonique"),
                         servir("bob-jeton", "bob-canonique"))

    assert vus["alice-jeton"] is not vus["bob-jeton"], (
        "les deux tâches ont tenu le MÊME objet : la portée n'est pas par message")


@pytest.mark.asyncio
async def test_deux_appels_concurrents_sur_la_chaine_servie_gardent_chacun_son_compte(
        compteur, deux_identites, monkeypatch):
    """La même preuve, mais sur la chaîne RÉELLEMENT servie et en concurrence.

    Le jeton est lu d'une ContextVar — c'est ainsi que le vrai serveur le porte
    (`fastmcp` le pose par requête) : deux appels menés en même temps dans le MÊME
    processus voient donc deux jetons différents. Chacun doit repartir avec SON
    identité canonique. C'est le scénario qui, s'il tournait mal, ferait servir le
    compte d'un utilisateur à un autre."""
    import contextvars

    import fastmcp.server.dependencies as deps
    from fastmcp import Client

    porteur: contextvars.ContextVar = contextvars.ContextVar("jeton_de_test", default=None)
    monkeypatch.setattr(deps, "get_access_token", lambda: porteur.get())

    banc = _chaine_servie(avec_portee=True)
    servis: dict[str, list[str]] = {}

    @banc.tool()
    def qui_suis_je() -> dict:
        return {"sub": hooks.current_user_sub_from_token()}

    async def appelle(jeton_sub: str):
        porteur.set(types.SimpleNamespace(
            claims={"sub": jeton_sub, "email": None, "name": None}))
        async with Client(banc) as c:
            for _ in range(3):
                r = await c.call_tool("qui_suis_je", {})
                servis.setdefault(jeton_sub, []).append(r.data["sub"])
                await asyncio.sleep(0.01)

    await asyncio.gather(appelle("alice-jeton"), appelle("bob-jeton"))

    assert servis["alice-jeton"] == ["alice-canonique"] * 3, servis
    assert servis["bob-jeton"] == ["bob-canonique"] * 3, servis


def test_hors_portee_le_chemin_est_celui_d_avant(deux_identites, monkeypatch):
    """Aucune portée ouverte ⟹ aucune mémoire : chaque appel repaie sa résolution,
    exactement comme avant l'existence de ce cache. C'est ce qui rend le correctif
    inerte pour tout ce qui n'est pas un message servi (script, timer, test)."""
    _porte(monkeypatch, "alice-jeton")
    assert hooks.current_user_sub_from_token() == "alice-canonique"
    assert hooks.current_user_sub_from_token() == "alice-canonique"
    assert deux_identites.vus == ["alice-jeton", "alice-jeton"], deux_identites.vus


def test_un_refus_ne_se_memorise_pas(monkeypatch):
    """`AliasNonResolvable` (chaîne qui n'aboutit à aucun compte vivant) doit se lever
    pour CHAQUE demandeur, comme avant. Un refus rangé dans le cache serait rendu en
    VALEUR au demandeur suivant — c'est-à-dire servi."""
    from oto_mcp import db
    from oto_mcp.db.sub_aliases import AliasNonResolvable

    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: True)
    appels: list[str] = []

    def resolve_sub(sub):
        appels.append(sub)
        raise AliasNonResolvable(sub, "compte_disparu", "le compte visé n'existe plus")

    monkeypatch.setattr(db, "resolve_sub", resolve_sub)
    _porte(monkeypatch, "fantome-jeton")

    with hooks.identity_scope():
        for _ in range(2):
            with pytest.raises(AliasNonResolvable):
                hooks.current_user_sub_from_token()
    assert appels == ["fantome-jeton", "fantome-jeton"], appels


@pytest.mark.asyncio
async def test_un_repli_ne_se_fait_pas_en_silence(monkeypatch, caplog):
    """Le repli est le bon comportement ; son SILENCE ne l'est pas.

    Si la pré-résolution tombe (base injoignable, exécuteur saturé), la plateforme
    retombe sur le chemin DANS la boucle — le gel même que ce lot ferme. Muet, le
    correctif se désarmerait tout seul et on croirait qu'il tient : c'est le motif
    qu'on passe la nuit à traquer.

    ⚠️ Le test passe par le MIDDLEWARE, pas par l'aide qui borne le débit. Une première
    rédaction appelait l'aide directement : elle restait verte quand on retirait le cri
    de la branche `except` — c'est-à-dire face au silence même qu'elle prétendait
    interdire. Mesuré, pas supposé.

    Le cri est aussi à DÉBIT BORNÉ, et ça se mesure : une base tombée fait passer chaque
    message ici, et un cri par message noierait le journal au moment précis où on a
    besoin de le lire. Le message qui sort porte donc le NOMBRE d'occurrences absorbées
    depuis le précédent — l'ampleur, qu'un cri par occurrence ne donnerait pas mieux."""
    import logging

    from oto_mcp.middleware import identity_scope as mod

    def tombe():
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(mod, "prime_identity", tombe)
    monkeypatch.setattr(mod, "_dernier_cri", 0.0)
    monkeypatch.setattr(mod, "_absorbes", 0)

    async def call_next(ctx):
        return "servi"

    ctx = types.SimpleNamespace(type="request", method="tools/call")
    mw = mod.IdentityScopeMiddleware()

    with caplog.at_level(logging.ERROR, logger=mod.__name__):
        assert await mw.on_message(ctx, call_next) == "servi", (
            "un échec de pré-résolution ne doit pas empêcher de servir le message")
        cris = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert cris, (
            "la pré-résolution est tombée, le message a été servi par le chemin dans la "
            "boucle, et RIEN n'a été journalisé : le correctif peut se désarmer sans "
            "témoin")
        assert "DANS la boucle" in cris[0].getMessage(), cris[0].getMessage()

        caplog.clear()
        for _ in range(50):
            await mw.on_message(ctx, call_next)
        assert not caplog.records, (
            f"{len(caplog.records)} messages pour 50 échecs rapprochés : le débit n'est "
            "pas borné, le journal se noie quand la base tombe")

        monkeypatch.setattr(mod, "_dernier_cri", 0.0)
        await mw.on_message(ctx, call_next)
        assert "51 occurrence(s)" in caplog.records[0].getMessage(), (
            "le message ne porte pas le nombre d'occurrences absorbées — le débit borné "
            "ferait alors perdre l'AMPLEUR en plus du détail : "
            + caplog.records[0].getMessage())


def test_une_pre_resolution_en_echec_rend_le_chemin_d_avant(monkeypatch):
    """La pré-résolution hors boucle est une OPTIMISATION : si elle tombe, le message
    continue et le premier vrai demandeur rejoue le chemin d'avant — au même endroit,
    avec la même exception, donc sous l'enveloppe d'erreur. Lever depuis le middleware
    (plus externe qu'`ErrorEnvelope`) changerait la destination du refus."""
    from oto_mcp import db
    from oto_mcp.middleware.identity_scope import IdentityScopeMiddleware

    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: True)
    monkeypatch.setattr(db, "resolve_sub", lambda sub: (_ for _ in ()).throw(
        RuntimeError("base injoignable")))
    _porte(monkeypatch, "alice-jeton")

    passe: dict = {}

    async def call_next(ctx):
        passe["portee_ouverte"] = hooks._identity_cache.get() is not None
        return "servi"

    ctx = types.SimpleNamespace(type="request", method="tools/call")
    assert asyncio.run(IdentityScopeMiddleware().on_message(ctx, call_next)) == "servi"
    assert passe["portee_ouverte"], (
        "la portée doit rester ouverte même quand la pré-résolution tombe : sinon le "
        "chemin paresseux repaierait sa résolution à chaque intermédiaire")
