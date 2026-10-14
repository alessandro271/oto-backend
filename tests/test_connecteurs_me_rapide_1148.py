"""`GET /api/me/connectors` : une lecture par fait et par appel (oto-backend#1148).

Après le passage sur une seule connexion (v1.434.0), la route restait à 1,75 s de
médiane en production (p95 4,41 s, 87 appels sur trois jours). Le profil sur un jeu
réaliste (`_jeu_connecteurs_1148.py` : trois orgs, trois équipes, des clés à tous les
paliers, des prêts, Google et Microsoft liés) : ~200 requêtes SQL par appel, dont 157
dans `access.status_for` — le lien Google relu pour chacun des services Google, l'org
active relue 28 fois, les réglages des connecteurs une fois par service Microsoft, le
rôle et la pause du prêteur à chaque passage. Aucun appel réseau sortant. À 3,14 ms
l'aller-retour vers la base managée, c'est ~0,77 s de requêtes par appel.

Le remède (`db.lecture_d_appel`) : une lecture pure de la base, décorée, ne se fait
qu'une fois par arguments dans une portée `db.reuse_connection()` — la portée qui
enveloppe déjà `connectors.me` et `status_for`, et qui meurt avec l'appel. Plus les
sessions navigateur, lues dans l'inventaire de chaque palier au lieu d'une requête par
connecteur et par palier.

Ce que ce fichier tient :
1. la réponse est STRICTEMENT la même, avec et sans la mémoire d'appel (même base,
   même processus, JSON comparé octet pour octet) — y compris `status_for`, que
   `oto_connector op=list` et `oto_list_my_tools` lisent par `credential_presence` ;
2. le nombre de requêtes par appel est borné : il ne suit ni le nombre de services
   d'une même famille au catalogue (Google, Microsoft — rouge sur le code d'avant), ni
   le nombre de connecteurs que le membre a branchés ;
3. aucun appel réseau sortant sur le chemin ;
4. la mémoire d'appel ne survit pas à sa portée, ne prête jamais un objet partagé, et
   s'efface devant une transaction explicite (`conn=`).
"""
from __future__ import annotations

import json
import os
import socket

import pytest

import _jeu_connecteurs_1148 as jeu_1148

# Le relevé du 09/10/2026 sur le jeu : 44 requêtes (liste verbeuse ou compacte), 50
# (lecture ciblée, qui calcule en plus l'aptitude de deux connecteurs), 25
# (`status_for`). Une marge, pas un chèque en blanc : avant, ~200.
# ⚠️ Ce relevé-là était fait sur un registre PARTIEL (rouge du tronc, 12/10/2026) : les
# flux de connexion (`connect.app_ready`) et les sondes d'état se déclarent à l'import
# du module de leur connecteur, que ce fichier seul n'importait pas. Dans un worker qui
# les avait importés — et en production, où le serveur les importe tous —, la liste
# verbeuse faisait 129 à 136 requêtes : l'`app_ready` des trois connecteurs Zoho
# demandait chacun la résolution complète (~30 requêtes). Le jeu charge désormais le
# registre du serveur (`_mcp_app.static_mcp`) : verbeuse 50, compacte 48.
_BORNE_LISTE = 55
_BORNE_CIBLEE = 65
_BORNE_STATUT = 32


@pytest.fixture(scope="module")
def jeu(live):
    # Le registre du SERVEUR (flux de connexion, sondes d'état), pas celui que les
    # tests joués avant dans le worker ont laissé : sans lui, le compte de requêtes
    # dépend de l'ordre de la suite, et mesure un chemin qui n'est pas celui servi.
    import _mcp_app
    _mcp_app.static_mcp()
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    try:
        yield jeu_1148.peupler()
    finally:
        if avant is None:
            os.environ.pop("OTO_MCP_MASTER_KEY", None)
        else:
            os.environ["OTO_MCP_MASTER_KEY"] = avant


def _appels(jeu) -> dict:
    from oto_mcp import access
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as S
    from oto_mcp.connectors import credential_presence
    ctx = ResolvedCtx(sub=jeu.sub, org_id=jeu.org)
    return {
        "verbeux": lambda: S._me(ctx, S.MyConnectorsInput(verbose=True)),
        "compact": lambda: S._me(ctx, S.MyConnectorsInput()),
        "cible": lambda: S._me(ctx, S.MyConnectorsInput(name="linkedin")),
        "filtre": lambda: S._me(ctx, S.MyConnectorsInput(state="active")),
        # Le chemin commun d'`oto_connector op=list` et d'`oto_list_my_tools`.
        "presence": lambda: credential_presence.lire(jeu.sub, org=jeu.org),
        "status_for": lambda: access.status_for(jeu.sub),
    }


class _Oublieuse(dict):
    """La mémoire d'une portée qui ne retient rien : chaque lecture repart à la base,
    exactement comme avant #1148."""

    def __contains__(self, cle) -> bool:
        return False


def _sans_memoire(monkeypatch):
    from oto_mcp.db import _conn
    reel = _conn._EmpruntParesseux.__init__

    def _init(self):
        reel(self)
        self.lectures = _Oublieuse()
    monkeypatch.setattr(_conn._EmpruntParesseux, "__init__", _init)


def _compter(f) -> tuple[object, int]:
    import psycopg
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        out = f()
    return out, len(vues)


def _json(valeur) -> str:
    # Pas de `sort_keys` : l'ORDRE des clés et des lignes fait partie de ce qu'on compare.
    return json.dumps(valeur, ensure_ascii=False, default=str)


def test_la_reponse_est_strictement_la_meme_avec_et_sans_memoire(jeu, monkeypatch):
    avec = {}
    for nom, f in _appels(jeu).items():
        out, n = _compter(f)
        avec[nom] = (_json(out), n)
    _sans_memoire(monkeypatch)
    for nom, f in _appels(jeu).items():
        out, n = _compter(f)
        assert _json(out) == avec[nom][0], f"{nom} : la réponse diffère avec la mémoire d'appel"
        # L'instrument mord : sans la mémoire, le même appel relit la base bien plus.
        assert n > 2 * avec[nom][1], (
            f"{nom} : {n} requêtes sans la mémoire, {avec[nom][1]} avec — le banc ne "
            "compare plus les deux chemins")


def test_les_sessions_lues_dans_l_inventaire_rendent_credential_status(jeu):
    """`statuts_mono_compte` remplace, dans `status_for`, une requête par connecteur à
    session et par palier : elle doit rendre, connecteur par connecteur, exactement
    ce que `credential_status` rend — présence comprise."""
    from oto_mcp import credentials_store as cs
    from oto_mcp import providers
    entites = [(cs.MEMBER, cs.member_id(jeu.org, jeu.sub)), ("group", str(jeu.equipe)),
               ("org", str(jeu.org)), (cs.MEMBER, cs.member_id(jeu.autre_org, jeu.sub))]
    presents = 0
    for et, eid in entites:
        lot = cs.statuts_mono_compte(et, eid)
        for nom in providers.REGISTRY:
            assert lot.get(nom) == cs.credential_status(et, eid, nom), (et, eid, nom)
            presents += nom in lot
    assert presents > 10, "le jeu ne pose presque aucune clé : la comparaison ne prouve rien"


@pytest.mark.parametrize("nom, borne", [
    ("verbeux", _BORNE_LISTE), ("compact", _BORNE_LISTE), ("filtre", _BORNE_LISTE),
    ("cible", _BORNE_CIBLEE), ("presence", _BORNE_STATUT), ("status_for", _BORNE_STATUT)])
def test_le_nombre_de_requetes_par_appel_est_borne(jeu, nom, borne):
    _, n = _compter(_appels(jeu)[nom])
    assert n <= borne, f"{nom} : {n} requêtes SQL pour un appel (borne {borne})"


def test_le_nombre_de_requetes_ne_suit_pas_le_nombre_de_connecteurs(jeu):
    """Dix connecteurs de plus branchés (clés perso et d'équipe, installés), un compte
    Google de plus : pas une requête de plus."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.connectors import selection as connector_selection
    appels = _appels(jeu)
    avant = {nom: _compter(appels[nom])[1] for nom in ("verbeux", "compact", "status_for")}
    perso = jeu_1148._eligibles("user")
    partage = jeu_1148._eligibles("org")
    moi = cs.member_id(jeu.org, jeu.sub)
    for nom in perso[19:29]:
        jeu_1148._cle(cs.MEMBER, moi, nom)
        connector_selection.set_state(jeu.sub, nom, connector_selection.ACTIVE, jeu.org)
    for nom in partage[24:28]:
        jeu_1148._cle("group", jeu.equipe, nom)
    jeu_1148._cle(cs.MEMBER, moi, "google", account="moi3@example.invalid",
                  meta={"scopes": "openid", "granted_at": "2026-09-04T00:00:00"})
    apres = {nom: _compter(appels[nom])[1] for nom in avant}
    assert apres == avant, f"le compte de requêtes suit les connecteurs branchés : {avant} → {apres}"


def test_le_registre_mesure_est_celui_du_serveur(jeu):
    """Le banc mesure le chemin SERVI : les flux qui posent `connect.app_ready` sur la
    liste verbeuse sont déclarés, Zoho compris (c'est lui qui pesait)."""
    from oto_mcp.connectors import flow as connector_flow
    assert {"zoho", "zohodesk", "zohoanalytics", "microsoft", "google"} <= set(
        connector_flow._FLOWS), sorted(connector_flow._FLOWS)


@pytest.mark.parametrize("apportee", [False, True])
def test_l_app_zoho_promise_est_celle_de_la_resolution(jeu, apportee):
    """`connect.app_ready` d'un connecteur Zoho : la présence répond d'abord, la
    résolution seulement si un credential est à portée — et la réponse est STRICTEMENT
    celle de `has_app`, sans app apportée comme avec une app apportée au palier membre."""
    from oto_mcp import access
    from oto_mcp import credentials_store as cs
    from oto_mcp import db
    from oto_mcp.auth import zoho as zoho_oauth
    moi = cs.member_id(jeu.org, jeu.sub)
    if apportee:
        cs.set_credential(cs.MEMBER, moi, "zohodesk", cs.pack_secret(
            "zohodesk", {"client_id": "1000.FACTICE", "client_secret": "s"}), set_by=jeu.sub)
    try:
        for nom in ("zoho", "zohodesk", "zohoanalytics"):
            with db.reuse_connection():
                promise = zoho_oauth.app_ready(nom, jeu.sub)
            assert promise is zoho_oauth.has_app(
                nom, jeu.sub, org=access.current_org(jeu.sub),
                group=access.current_group(jeu.sub)), nom
        if apportee:
            with db.reuse_connection():
                assert zoho_oauth.app_ready("zohodesk", jeu.sub) is True
    finally:
        if apportee:
            cs.clear_credential(cs.MEMBER, moi, "zohodesk")


def test_un_service_de_plus_ne_coute_aucune_requete(jeu):
    """Le poste qui pesait le plus : le lien de chaque service Google et Microsoft (et
    l'étape manquante de chaque service Microsoft), relu service par service sur les
    mêmes comptes. Lire toute la famille coûte désormais ce que coûte sa tête."""
    from oto_mcp import db, status_hints
    from oto_mcp.auth import google as auth_google
    from oto_mcp.auth import microsoft as auth_microsoft
    from oto_mcp.connectors import link as connector_link
    for tete, services in (("google", auth_google.SERVICES),
                           (auth_microsoft.CONNECTOR, auth_microsoft.SERVICES)):
        famille = (tete, *services)

        def _lire(noms):
            def _f():
                for nom in noms:
                    connector_link.state(nom, jeu.sub)
                    if status_hints.has_hook(nom):
                        status_hints.pending_action(nom, jeu.sub, jeu.org, jeu.equipe, {})
            with db.reuse_connection():
                return _compter(_f)[1]
        assert _lire(famille) == _lire((tete,)), f"chaque service de {tete} relit la base"


def test_aucun_appel_reseau_sur_le_chemin(jeu, monkeypatch):
    """Une liste ne parle à aucun fournisseur (statut, jeton) : elle lit un état connu.
    La base de test est locale ; tout le reste est coupé, et relevé."""
    sorties = []
    reel_connect, reel_gai = socket.socket.connect, socket.getaddrinfo
    locaux = ("127.0.0.1", "::1", "localhost")

    def _connect(self, adresse, *a, **k):
        if isinstance(adresse, (str, bytes)) or adresse[0] in locaux:
            return reel_connect(self, adresse, *a, **k)
        sorties.append(adresse)
        raise OSError(f"réseau coupé par le banc : {adresse}")

    def _gai(hote, *a, **k):
        if hote in locaux or hote is None:
            return reel_gai(hote, *a, **k)
        sorties.append(("dns", hote))
        raise OSError(f"DNS coupé par le banc : {hote}")
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    for f in _appels(jeu).values():
        f()
    assert not sorties, f"appel réseau sortant sur le chemin de la liste : {sorties}"
    # Le piège mord : une sortie réelle est bien vue (sinon il ne regarde rien).
    import urllib.request
    with pytest.raises(OSError):
        urllib.request.urlopen("http://fournisseur.example.invalid/", timeout=1)
    assert sorties, "le piège réseau ne voit pas une sortie réelle"


def test_la_memoire_d_appel_meurt_avec_sa_portee(jeu):
    from oto_mcp import db, group_store, org_store
    from oto_mcp.db import _conn
    from oto_mcp.db import connector_settings

    # Hors portée : chaque appel lit la base.
    _, n = _compter(lambda: [org_store.get_active_org(jeu.sub) for _ in range(3)])
    assert n == 3
    # Dans la portée : une lecture par arguments, et chaque appelant reçoit SA copie.
    with db.reuse_connection():
        _, n = _compter(lambda: [org_store.get_active_org(jeu.sub) for _ in range(3)])
        assert n == 1
        premiere = group_store.list_groups_for_user(jeu.sub, jeu.org)
        premiere.clear()
        assert group_store.list_groups_for_user(jeu.sub, jeu.org), (
            "un appelant qui modifie sa réponse ne doit pas la modifier pour le suivant")
        # Une transaction explicite (`conn=`) lit toujours la base : elle peut écrire.
        with _conn._connect() as c:
            _, n = _compter(lambda: [connector_settings.list_connector_settings(conn=c)
                                     for _ in range(2)])
        assert n == 2
    assert _conn._emprunt_partage.get() is None
    # Une écriture faite ENTRE deux appels est vue par le suivant : rien ne survit.
    org_store.set_active_org(jeu.sub, jeu.autre_org)
    try:
        with db.reuse_connection():
            assert org_store.get_active_org(jeu.sub) == jeu.autre_org
    finally:
        org_store.set_active_org(jeu.sub, jeu.org)
    with db.reuse_connection():
        assert org_store.get_active_org(jeu.sub) == jeu.org
