"""Lire les droits déclarés depuis la console d'administration (oto-app#13, lot A),
sur une vraie base.

Ce que ces bancs tiennent :
- **la liste transversale** (`GET /api/admin/entitlements`, `oto_admin_org
  op=entitlements`) : les trois portées, dons faits à une personne partout compris ;
  chaque filtre, seul et combiné ; une pagination sans trou ni doublon ; les refus
  nommés ; la route servie, réservée à l'admin plateforme ;
- **la fiche d'org admin** porte les droits de l'org, échus compris, à côté de
  `billing` — la fiche d'un membre n'en dit rien ;
- **la fiche d'un compte** porte les droits posés sur la personne, datés et signés ;
- **le siège unipile** dit ce qui ouvre son droit, par le chemin de `has_right` ;
- **la garde** : aucune de ces lectures ne touche les dons d'option, les abonnements ni
  la facturation du cœur, qui partent avec son retrait (oto-backend#1104) — et chacune
  tient en un nombre de requêtes qui ne dépend pas du nombre de lignes.

Identifiants factices, org et étiquette de source propres à chaque test : la base du
module est partagée par ses tests.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp import access
from oto_mcp import entitlements_catalogue as C
from oto_mcp.capabilities import (_rest_adapter, admin_console, droits_admin,
                                  unipile_seats, users_admin)
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.orgs import reads as orgs_reads
from oto_mcp.capabilities.registry import by_key
from oto_mcp.db import entitlements as E
from oto_mcp.db._conn import _connect

MAINTENANT = datetime.now(timezone.utc)
HIER = MAINTENANT - timedelta(days=1)
DEMAIN = MAINTENANT + timedelta(days=1)
JADIS = datetime(2020, 1, 1, tzinfo=timezone.utc)
CTX = ResolvedCtx(sub="admin-banc")

# Les tables qui partent avec la facturation du cœur : aucune lecture neuve n'y touche.
_HERITEES = ("option_comps", "org_subscriptions", "billing")


def _org(created_at=None) -> int:
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO orgs (name, created_at) VALUES (%s, COALESCE(%s, NOW())) "
            "RETURNING id", (f"org-{uuid.uuid4().hex[:8]}", created_at)).fetchone()["id"]


def _compte(role: str = "member") -> str:
    sub = f"u-{uuid.uuid4().hex[:8]}"
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub, email, role) VALUES (%s, %s, %s)",
                     (sub, f"{sub}@example.test", role))
    return sub


class Banc:
    """Quatre lignes sous une étiquette propre au test, une par cas de lecture :
    - org ancienne, ligne d'org `unipile` vivante, sans échéance ;
    - la même org, une personne dans l'org, `unipile_seats` vivante jusqu'à demain ;
    - une personne partout, `platform_unmetered` ÉCHUE hier ;
    - org récente, ligne d'org `unipile` à 0 qui ne commence que demain."""

    def __init__(self):
        self.source = f"banc-{uuid.uuid4().hex[:8]}"
        self.ancienne, self.recente = _org(JADIS), _org()
        self.membre, self.partout = _compte(), _compte()
        E.grant(self.ancienne, C.UNIPILE, self.source, value=1, granted_by="service:banc")
        E.grant(self.ancienne, C.UNIPILE_SEATS, self.source, value=7, sub=self.membre,
                expires_at=DEMAIN, granted_by="service:banc")
        E.grant(None, C.PLATFORM_UNMETERED, self.source, value=1, sub=self.partout,
                starts_at=HIER - timedelta(days=2), expires_at=HIER,
                granted_by="service:banc")
        E.grant(self.recente, C.UNIPILE, self.source, value=0, starts_at=DEMAIN,
                granted_by="service:banc")

    def lister(self, **filtres) -> dict:
        return droits_admin._lister(CTX, droits_admin.EntitlementsListInput(
            source=self.source, **filtres))

    def cles(self, **filtres) -> set[tuple]:
        return {(r["org_id"], r["sub"], r["right_key"])
                for r in self.lister(**filtres)["entitlements"]}

    @property
    def org_ancienne(self):
        return (self.ancienne, None, C.UNIPILE)

    @property
    def dans_l_org(self):
        return (self.ancienne, self.membre, C.UNIPILE_SEATS)

    @property
    def don_partout(self):
        return (None, self.partout, C.PLATFORM_UNMETERED)

    @property
    def org_recente(self):
        return (self.recente, None, C.UNIPILE)


@pytest.fixture
def banc(live):
    return Banc()


# ── la liste transversale ─────────────────────────────────────────────────────

def test_la_liste_couvre_les_trois_portees_dons_a_une_personne_compris(banc):
    page = banc.lister()
    par_cle = {(r["org_id"], r["sub"], r["right_key"]): r for r in page["entitlements"]}
    assert set(par_cle) == {banc.org_ancienne, banc.dans_l_org, banc.don_partout,
                            banc.org_recente}
    assert page["count"] == 4 and page["next_cursor"] is None
    assert par_cle[banc.org_ancienne]["scope"] == "org"
    assert par_cle[banc.dans_l_org]["scope"] == "personne_dans_org"
    assert par_cle[banc.don_partout]["scope"] == "personne_partout"
    assert par_cle[banc.dans_l_org]["expires_at"] is not None
    assert par_cle[banc.don_partout]["active"] is False, "échue hier"
    assert par_cle[banc.org_recente]["active"] is False, "ne commence que demain"
    assert par_cle[banc.org_ancienne]["active"] is True


def test_la_vue_resserree_par_defaut_et_complete_sur_demande(banc):
    (compacte,) = banc.lister(scope="personne_partout")["entitlements"]
    assert not set(droits_admin._COMPLETS) & set(compacte), compacte
    (complete,) = banc.lister(scope="personne_partout", full=True)["entitlements"]
    assert complete["email"] == f"{banc.partout}@example.test"
    assert complete["granted_by"] == "service:banc" and complete["granted_at"]
    assert complete["org_name"] is None, "une personne partout n'a pas d'org"
    (d_org,) = banc.lister(scope="org", org_id=banc.ancienne, full=True)["entitlements"]
    assert d_org["org_name"].startswith("org-")
    assert d_org["org_created_at"].startswith("2020-01-01")


def test_chaque_filtre_seul(banc):
    assert banc.cles(right=C.UNIPILE) == {banc.org_ancienne, banc.org_recente}
    assert banc.cles(scope="org") == {banc.org_ancienne, banc.org_recente}
    assert banc.cles(scope="personne_dans_org") == {banc.dans_l_org}
    assert banc.cles(scope="personne_partout") == {banc.don_partout}
    assert banc.cles(scope="personne") == {banc.dans_l_org, banc.don_partout}
    assert banc.cles(active=True) == {banc.org_ancienne, banc.dans_l_org}
    assert banc.cles(active=False) == {banc.don_partout, banc.org_recente}, (
        "active=false = échues ET à venir")
    assert banc.cles(expires_before=DEMAIN + timedelta(hours=1)) == {
        banc.dans_l_org, banc.don_partout}, "une ligne sans échéance n'y est jamais"
    assert banc.cles(org_created_after=JADIS + timedelta(days=1)) == {banc.org_recente}, (
        "une ligne sans org ne passe jamais un filtre sur la date de l'org")
    assert banc.cles(org_id=banc.ancienne) == {banc.org_ancienne, banc.dans_l_org}


def test_les_filtres_se_combinent(banc):
    assert banc.cles(right=C.UNIPILE, active=True) == {banc.org_ancienne}
    assert banc.cles(scope="personne", active=False) == {banc.don_partout}
    assert banc.cles(right=C.UNIPILE, org_created_after=JADIS + timedelta(days=1),
                     active=True) == set()


def test_une_date_sans_fuseau_est_lue_en_utc(banc):
    naif = (DEMAIN + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    inp = droits_admin.EntitlementsListInput(source=banc.source, expires_before=naif)
    assert inp.expires_before.tzinfo is timezone.utc
    assert banc.cles(expires_before=naif) == {banc.dans_l_org, banc.don_partout}


def test_la_pagination_rend_tout_sans_trou_ni_doublon(banc):
    entier = banc.lister()["entitlements"]
    vus, cursor, pages = [], None, 0
    while True:
        page = banc.lister(limit=1, cursor=cursor)
        assert page["count"] == len(page["entitlements"]) == 1
        vus += page["entitlements"]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "la pagination ne termine pas"
    assert vus == entier, "même ordre, même contenu, page après page"
    assert vus[0]["org_id"] is None, "les lignes de personne partout en tête"


def test_la_page_est_ecretee_jamais_refusee(banc):
    assert droits_admin.EntitlementsListInput(limit=10_000).limit == droits_admin.PAGE_MAX
    assert droits_admin.EntitlementsListInput(limit=-3).limit == 1
    assert droits_admin.EntitlementsListInput().limit == droits_admin.PAGE


@pytest.mark.parametrize("cursor", ["pas-du-base64!", "e30", "WzEsMiwzXQ"])
def test_un_curseur_illisible_est_refuse_nomme(banc, cursor):
    with pytest.raises(AuthzDenied) as e:
        banc.lister(cursor=cursor)
    assert (e.value.status, e.value.code) == (400, "invalid_cursor")


def test_une_cle_hors_catalogue_est_refusee_nommee(banc):
    for cle in ("unipil", "platform_key:connecteur-inexistant"):
        with pytest.raises(AuthzDenied) as e:
            banc.lister(right=cle)
        assert (e.value.status, e.value.code) == (400, "entitlement_unknown_key")
    assert banc.cles(right=C.platform_key("serper")) == set(), "une clé valide passe"


def _rest(sub: str, query: str) -> tuple[int, dict]:
    cap = by_key("admin.entitlement.list")

    def _json_error(_req, status, code, message=None, **_kw):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse(payload, status_code=status)

    async def _auth(_req, _verifier, **_kw):
        return sub, None

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    handler = _rest_adapter._make_handler(cap, cap.rest, None, _auth,
                                          _json_response, _json_error)
    rep = asyncio.run(handler(Request(
        {"type": "http", "method": "GET", "path": "/api/admin/entitlements",
         "headers": [], "query_string": query.encode(), "path_params": {}}, _receive)))
    return rep.status_code, json.loads(bytes(rep.body))


def test_la_route_servie_filtre_par_la_query_string_et_reste_a_l_admin(banc):
    admin, membre = _compte("admin"), _compte()
    q = (f"source={banc.source}&scope=personne&active=false"
         f"&expires_before={(DEMAIN + timedelta(days=1)).date().isoformat()}&full=true")
    code, corps = _rest(admin, q)
    assert code == 200, corps
    (ligne,) = corps["entitlements"]
    assert (ligne["sub"], ligne["right_key"], ligne["granted_by"]) == (
        banc.partout, C.PLATFORM_UNMETERED, "service:banc")
    code, corps = _rest(admin, f"source={banc.source}&limit=3")
    assert code == 200 and corps["count"] == 3 and corps["next_cursor"]
    code, corps = _rest(admin, f"source={banc.source}&cursor={corps['next_cursor']}")
    assert code == 200 and corps["count"] == 1 and corps["next_cursor"] is None
    code, corps = _rest(admin, "right=pas-une-cle")
    assert (code, corps["error"]) == (400, "entitlement_unknown_key")
    code, corps = _rest(admin, "cursor=illisible!")
    assert (code, corps["error"]) == (400, "invalid_cursor")
    code, _ = _rest(membre, f"source={banc.source}")
    assert code == 403, "une lecture d'admin plateforme, comme oto_admin_org"


def test_la_console_mcp_rend_la_meme_lecture(banc):
    page = admin_console._org(CTX, admin_console.OrgAdminInput(
        op="entitlements", source=banc.source, scope="org", full=True))
    assert page == banc.lister(scope="org", full=True)
    une = admin_console._org(CTX, admin_console.OrgAdminInput(
        op="entitlements", source=banc.source, limit=1))
    assert une["count"] == 1 and une["next_cursor"]
    # Même contrôle d'accès que la fiche d'org admin : un admin plateforme passe, un
    # membre est refusé, sur les deux verbes.
    regle = by_key("admin.org").authz
    admin, membre = _compte("admin"), _compte()
    for op in ("entitlements", "get"):
        inp = admin_console.OrgAdminInput(op=op, org_id=banc.ancienne)
        assert regle(RawCtx(sub=admin), inp).sub == admin
        with pytest.raises(AuthzDenied) as e:
            regle(RawCtx(sub=membre), inp)
        assert e.value.status == 403, op


# ── les fiches ────────────────────────────────────────────────────────────────

def test_la_fiche_d_org_admin_porte_ses_droits_echus_compris(banc):
    E.grant(banc.ancienne, C.PLATFORM_UNMETERED, banc.source, value=1,
            starts_at=HIER - timedelta(days=1), expires_at=HIER, granted_by="service:banc")
    fiche = orgs_reads._org_admin_detail(CTX, orgs_reads.OrgIdInput(org_id=banc.ancienne))
    assert "billing" in fiche and "option_comps" in fiche, "à côté de billing, pas dedans"
    lignes = {(r["sub"], r["right_key"]): r for r in fiche["entitlements"]}
    assert set(lignes) == {(None, C.UNIPILE), (banc.membre, C.UNIPILE_SEATS),
                           (None, C.PLATFORM_UNMETERED)}
    echue = lignes[(None, C.PLATFORM_UNMETERED)]
    assert echue["active"] is False and echue["expires_at"]
    for r in fiche["entitlements"]:
        assert {"right_key", "value", "source", "starts_at", "expires_at",
                "granted_by", "granted_at"} <= set(r)
        assert r["granted_by"] == "service:banc"
    orgs_reads.OrgAdminDetail.model_validate(fiche)
    membre = orgs_reads._org_detail(CTX, orgs_reads.OrgIdInput(org_id=banc.ancienne))
    assert "entitlements" not in membre, "la fiche d'un membre ne dit rien des droits"


def test_la_fiche_d_un_compte_porte_ses_droits_dates_et_signes(banc):
    fiche = users_admin._user_detail(CTX, users_admin.UserGetInput(target=banc.partout))
    (ligne,) = fiche["entitlements"]
    assert (ligne["scope"], ligne["right_key"], ligne["source"], ligne["granted_by"]) == (
        "personne_partout", C.PLATFORM_UNMETERED, banc.source, "service:banc")
    assert ligne["expires_at"] and ligne["granted_at"] and ligne["active"] is False
    (dans,) = droits_admin.droits_de_la_personne(banc.membre)
    assert (dans["org_id"], dans["scope"], dans["value"]) == (
        banc.ancienne, "personne_dans_org", 7)


# ── le siège unipile : ce qui ouvre son droit ─────────────────────────────────

def _binding(sub: str, org: int) -> dict:
    return {"sub": sub, "org_id": org, "disconnected_at": None}


def test_le_siege_dit_quelle_ligne_ouvre_son_droit(banc):
    titulaire = _compte()
    ouvert = unipile_seats._ouverture([_binding(titulaire, banc.ancienne)])
    assert ouvert == {"sub": titulaire, "org_id": banc.ancienne,
                      "instance_default": False,
                      "rows": [{"scope": "org", "source": banc.source, "value": 1,
                                "expires_at": None}]}
    # Un don à la personne partout ouvre là où l'org n'a rien de valide ; son échéance
    # est dite.
    E.grant(None, C.UNIPILE, banc.source, value=1, sub=titulaire, expires_at=DEMAIN)
    ouvert = unipile_seats._ouverture([_binding(titulaire, banc.recente)])
    ((ligne,),) = [ouvert["rows"]]
    assert (ligne["scope"], ligne["source"]) == ("personne_partout", banc.source)
    assert ligne["expires_at"] is not None
    assert unipile_seats._ouverture([_binding(_compte(), banc.recente)]) is None


def test_le_siege_lit_par_le_chemin_de_has_right(banc, monkeypatch):
    titulaire = _compte()
    E.grant(None, C.UNIPILE, banc.source, value=1, sub=titulaire, expires_at=DEMAIN)
    for sub in (titulaire, banc.membre, _compte()):
        for org in (banc.ancienne, banc.recente):
            ouvert = unipile_seats._ouverture([_binding(sub, org)])
            assert (ouvert is not None) is access.has_right(sub, org, C.UNIPILE), (sub, org)
    # Le défaut d'instance qui ouvre se dit, sans ligne.
    monkeypatch.setenv("OTO_ENTITLEMENT_DEFAULTS", json.dumps({
        "unipile": 1, "platform_unmetered": 0, "unipile_seats": 5,
        "members_max": "unlimited", "platform_key:*": 0}))
    ouvert = unipile_seats._ouverture([_binding(_compte(), _org())])
    assert ouvert["instance_default"] is True and ouvert["rows"] == []


# ── la garde : rien d'hérité, un nombre de requêtes fixe ──────────────────────

def _requetes(f) -> tuple[object, list[str]]:
    import psycopg
    from unittest import mock
    vues: list[str] = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(str(query))
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        out = f()
    return out, vues


def test_aucune_lecture_neuve_ne_touche_l_heritage_de_la_facturation(banc):
    """`option_comps`, `org_subscriptions` et `billing*` partent du cœur (#1104) : une
    lecture qui s'y appuierait partirait avec eux. Jugé sur le SQL réellement exécuté,
    pas sur le source."""
    lectures = {
        "liste": lambda: banc.lister(full=True),
        "liste filtrée": lambda: banc.lister(right=C.UNIPILE, scope="personne",
                                             active=True, expires_before=DEMAIN,
                                             org_created_after=JADIS),
        "fiche d'org": lambda: droits_admin.droits_de_l_org(banc.ancienne),
        "fiche de compte": lambda: droits_admin.droits_de_la_personne(banc.membre),
        "siège": lambda: unipile_seats._ouverture([_binding(banc.membre, banc.ancienne)]),
    }
    for nom, f in lectures.items():
        _, vues = _requetes(f)
        assert vues, f"{nom} : aucune requête vue — l'instrument regarde à côté"
        fautives = [q for q in vues if any(t in q for t in _HERITEES)]
        assert not fautives, f"{nom} lit l'héritage de la facturation : {fautives}"


def test_le_nombre_de_requetes_ne_depend_pas_du_nombre_de_lignes(banc):
    for _ in range(5):
        E.grant(_org(), C.UNIPILE, banc.source, value=1)
    _, petite = _requetes(lambda: banc.lister(limit=1, full=True))
    _, grande = _requetes(lambda: banc.lister(limit=500, full=True))
    assert len(petite) == len(grande) == 1, (petite, grande)
    for f in (lambda: droits_admin.droits_de_l_org(banc.ancienne),
              lambda: droits_admin.droits_de_la_personne(banc.membre)):
        _, vues = _requetes(f)
        assert len(vues) == 1, vues
    # Le siège : une lecture par (titulaire, org), puis le cache de l'inventaire.
    cache: dict = {}
    rows = [_binding(banc.membre, banc.ancienne)]
    _, premiere = _requetes(lambda: unipile_seats._ouverture(rows, cache))
    _, seconde = _requetes(lambda: unipile_seats._ouverture(rows, cache))
    assert (len(premiere), len(seconde)) == (1, 0), (premiere, seconde)
