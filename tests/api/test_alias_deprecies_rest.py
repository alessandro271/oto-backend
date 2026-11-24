"""Un chemin REST renommé répond encore — en **308**, déprécié et sans date.

Les alias sont DÉRIVÉS des routes montées (`deprecations.alias_rest_derives` : datastore,
connecteur API). Ceux du lot B2 de #519 (`/api/me/doctrines/*`) sont RETIRÉS avec le
lot D (#526, 23/11/2026) : leur garde est `tests/api/test_retrait_lot_d.py`. Renommer un chemin, c'est casser tout appelant qui vit hors de ce
dépôt : le build de la vitrine, un `fetch` de navigateur, une intégration tierce, un
`curl` dans un script. Alors l'ancien chemin reste monté, ne fait rien d'autre que
rediriger, et s'en ira quand son usage compté sera nul (lot D, #526 ; décision
du 23/11/2026) : chaque redirection laisse sa ligne dans `tool_calls`.

Ce que ces tests gardent, dans l'ordre de ce qui coûterait le plus cher :

1. **Un 308, pas un 301.** Un 301/302 autorise le client à retomber en GET : un
   `POST …/publish` deviendrait un GET sur la nouvelle route — 405, ou pire, un
   no-op silencieux. Le 308 conserve la méthode et le corps.
2. **La query string survit.** Un client appelle `…/library?limit=200` ; un
   `Location` qui la perdrait rendrait 100 entrées au lieu de 200, sans qu'aucun
   code d'erreur ne le dise. C'est le genre de régression qu'on découvre au trafic.
3. **La cible EXISTE.** Un alias qui pointe un chemin mort est pire qu'un retrait
   sec : le client suit la redirection et reçoit un 404 qui ne nomme rien.
4. **Les en-têtes CORS sont sur la REDIRECTION.** Un navigateur vérifie CORS sur
   chaque réponse d'une chaîne : une 308 nue fait échouer le `fetch` cross-origin de
   la vitrine. Le préflight `OPTIONS`, lui, n'est jamais redirigé — sinon le
   navigateur abandonne avant d'essayer.
5. **Les alias sont montés EN DERNIER, et aucun n'éclipse une vraie route.** Un
   placeholder trop gourmand posé trop haut avalerait un chemin littéral servi.
6. **Le document OpenAPI les dit dépréciés**, avec le chemin de remplacement, sans
   date — et sans voler l'`operationId` d'une autre entrée.
7. **Chaque redirection est comptée** au journal `tool_calls` (`kind='rest'`) : `tool`
   = le chemin d'aujourd'hui, `args.deprecated_name` = l'ancien GABARIT, jamais le
   chemin réel.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import deprecations, openapi
from oto_mcp.api import routes as api_routes

_ORIGINE = "https://oto.cx"          # une origine réellement autorisée (`_allowed_origins`)


class _FauxVerifieur:
    """`make_routes` ne fait que CAPTURER le verifier — jamais appelé au montage."""


def _table():
    return api_routes.make_routes(_FauxVerifieur(), mcp_instance=None)


@pytest.fixture(scope="module")
def client():
    return TestClient(Starlette(routes=_table()))


def _resout(table, methode: str, chemin: str):
    """La route que Starlette servirait pour (méthode, chemin) — la PREMIÈRE qui
    matche entièrement, exactement comme le routeur."""
    scope = {"type": "http", "method": methode, "path": chemin, "headers": [],
             "path_params": {}, "root_path": "", "query_string": b""}
    for r in table:
        m, child = r.matches(scope)
        if m.name == "FULL":
            return r, child.get("path_params", {})
    return None, {}


def _exemple(chemin: str) -> str:
    """Un chemin concret : chaque placeholder reçoit une valeur plausible."""
    return chemin.replace("{slug}", "un-slug").replace("{id}", "42") \
                 .replace("{namespace}", "leads").replace("{datastore}", "leads") \
                 .replace("{row_id}", "7").replace("{comment_id}", "9") \
                 .replace("{scope}", "org")


_ALIAS = deprecations.alias_rest_derives()


# ── 1 & 2. 308, méthode conservée, query string reportée ────────────────────

@pytest.mark.parametrize("alias", _ALIAS, ids=lambda a: f"{a.verbe} {a.ancien}")
def test_lancien_chemin_redirige_en_308_vers_le_nouveau(client, alias):
    r = client.request(alias.verbe, _exemple(alias.ancien), follow_redirects=False)
    assert r.status_code == 308, (
        f"{alias.verbe} {alias.ancien} répond {r.status_code}. Un 301/302 autorise le "
        "client à retomber en GET — sur un POST, c'est un no-op silencieux.")
    assert r.headers["location"] == _exemple(alias.nouveau)


def test_la_query_string_est_reportee(client):
    """Un client qui appelle `…/rows?limit=200` : la perdre rendrait 100 lignes au
    lieu de 200, sans le moindre code d'erreur."""
    r = client.get("/api/datastore/namespaces/leads/rows?limit=200&q=x",
                   follow_redirects=False)
    assert r.headers["location"] == "/api/datastores/leads/rows?limit=200&q=x"


def test_lavis_de_depreciation_est_dans_les_en_tetes_sans_date(client):
    """Un intégrateur qui lit ses logs voit la dépréciation sans ouvrir la doc — et
    aucune date de retrait (décision du 23/11/2026)."""
    r = client.get("/api/datastore/namespaces/leads/rows", follow_redirects=False)
    assert r.headers["deprecation"] == "true"
    assert "sunset" not in r.headers


# ── 3. La cible existe et n'est pas, elle-même, un alias ────────────────────

@pytest.mark.parametrize("alias", _ALIAS, ids=lambda a: a.nouveau)
def test_la_cible_de_lalias_est_une_route_servie(alias):
    table = _table()
    route, _ = _resout(table, alias.verbe, _exemple(alias.nouveau))
    assert route is not None, (
        f"`{alias.nouveau}` n'est servi par aucune route : la redirection mène à un "
        "404 qui ne nomme rien. Pire qu'un retrait sec.")
    assert getattr(route.endpoint, "__name__", "") != "alias_deprecie", (
        f"`{alias.nouveau}` est lui-même un alias : une chaîne de redirections, dont "
        "un maillon partira au lot D en cassant l'autre.")


# ── 4. Le préflight n'est jamais redirigé ───────────────────────────────────

@pytest.mark.parametrize("alias", _ALIAS, ids=lambda a: a.ancien)
def test_le_preflight_nest_pas_redirige(alias):
    """Un `OPTIONS` qui répondrait 308 ferait abandonner le navigateur avant même
    d'essayer la requête réelle."""
    route, _ = _resout(_table(), "OPTIONS", _exemple(alias.ancien))
    assert route is not None
    assert getattr(route.endpoint, "__name__", "") == "options_handler"


def test_la_redirection_porte_les_en_tetes_cors(client):
    """Le navigateur vérifie CORS sur CHAQUE réponse d'une chaîne de redirections."""
    r = client.get("/api/datastore/namespaces/leads/rows", follow_redirects=False,
                   headers={"origin": _ORIGINE})
    assert r.headers.get("access-control-allow-origin") == _ORIGINE


def test_sans_origine_pas_den_tete_cors(client):
    """Inertie : un appel serveur-à-serveur reçoit
    exactement ce qu'il recevait — pas d'en-tête inventé."""
    r = client.get("/api/datastore/namespaces/leads/rows", follow_redirects=False)
    assert "access-control-allow-origin" not in r.headers


# ── 5. Les alias sont derniers, et n'éclipsent rien ─────────────────────────

def test_les_alias_sont_montes_en_dernier():
    """Un alias ne doit pouvoir capturer que ce que rien d'autre ne sert. Monté plus
    haut, un de ses placeholders éclipserait une vraie route en silence."""
    table = _table()
    positions = [i for i, r in enumerate(table)
                 if getattr(r.endpoint, "__name__", "") == "alias_deprecie"]
    assert positions, "aucun alias monté — table vide ou renommage du handler"
    premier = min(positions)
    apres = [r.path for r in table[premier:]
             if getattr(r.endpoint, "__name__", "") not in
             ("alias_deprecie", "options_handler")]
    assert not apres, (
        f"{apres} sont montées APRÈS le premier alias déprécié. Les alias se montent "
        "en dernier (cf. `routes.make_routes`) — sinon un placeholder d'alias peut "
        "avaler un chemin littéral servi juste en dessous.")


@pytest.mark.parametrize("alias", _ALIAS, ids=lambda a: f"{a.verbe} {a.ancien}")
def test_chaque_ancien_chemin_atteint_bien_son_alias(alias):
    """Le cas qui mord : un chemin littéral DOIT atteindre son alias, et non être
    avalé par un placeholder voisin qui capture un segment (`…/rows/batch` contre
    `…/rows/{row_id}`)."""
    route, _ = _resout(_table(), alias.verbe, _exemple(alias.ancien))
    assert route is not None and route.path == alias.ancien, (
        f"{alias.verbe} {alias.ancien} est servi par `{route.path if route else None}` : "
        "un chemin littéral doit précéder le placeholder qui l'engloberait.")


# ── 6. Le document le dit, avec le remplaçant et sans date ──────────────────

@pytest.mark.parametrize("alias", _ALIAS, ids=lambda a: a.ancien)
def test_lopenapi_marque_lancien_chemin_deprecie(alias):
    doc = openapi.build()
    op = doc["paths"][alias.ancien][alias.verbe.lower()]
    assert op["deprecated"] is True
    assert alias.nouveau in op["summary"], (
        "« déprécié » sans chemin de remplacement n'est qu'un reproche")
    assert "préavis" in op["summary"]
    assert deprecations.REVUE.strftime("%d/%m/%Y") not in op["summary"] + op["description"]
    assert "308" in op["responses"]
    assert "Sunset" not in op["responses"]["308"]["headers"]


def test_les_nouveaux_chemins_sont_documentes():
    doc = openapi.build()
    manquants = [a.nouveau for a in _ALIAS if a.nouveau not in doc["paths"]]
    assert not manquants, f"chemins d'aujourd'hui absents du document : {manquants}"


def test_un_alias_ne_vole_pas_loperationid_dune_autre_entree():
    """Un `operationId` est unique dans un document OpenAPI : deux entrées qui le
    partagent font disparaître une méthode d'un client généré, en silence.

    L'unicité sur TOUT le document est gardée par `tests/test_openapi.py` (#436) ;
    celle-ci nomme la règle propre aux alias quand elle est enfreinte."""
    doc = openapi.build()
    anciens = {a.ancien for a in _ALIAS}
    ids_alias = {op["operationId"] for p, item in doc["paths"].items() if p in anciens
                 for op in item.values()}
    ids_autres = {op["operationId"] for p, item in doc["paths"].items()
                  if p not in anciens for op in item.values()}
    assert not (ids_alias & ids_autres), (
        f"{sorted(ids_alias & ids_autres)} : un alias déprécié porte l'operationId "
        "d'une entrée vivante : l'alias reçoit un id dérivé de son chemin.")


# ── 7. Chaque redirection laisse sa ligne au journal ────────────────────────

def test_chaque_redirection_est_comptee_au_journal_sans_le_chemin_reel(monkeypatch):
    """La mesure qui décide du retrait ne dépend plus des 7 jours du journal du
    proxy : la 308 écrit sa ligne par le chemin habituel du journal des routes
    (`RestCallLogger`, écriture asynchrone), sans authentifier ni requêter."""
    from starlette.middleware import Middleware

    from oto_mcp.api import alias_routes
    from oto_mcp.api import base as api_base

    lignes: list = []

    def _emet(row):
        lignes.append(dict(row))

        async def _fait():
            return None
        return _fait()

    monkeypatch.setattr(api_routes, "_emit_rest_event", _emet)
    app = Starlette(routes=alias_routes.make_routes(api_base.options_handler),
                    middleware=[Middleware(api_routes.RestCallLogger)])
    alias = next(a for a in _ALIAS
                 if a.verbe == "GET" and a.ancien.endswith("/rows/{row_id}"))
    r = TestClient(app).request(alias.verbe, "/api/datastore/namespaces/leads/rows/957",
                                headers={"X-Oto-Org": "12"}, follow_redirects=False)
    assert r.status_code == 308
    (ligne,) = lignes
    assert ligne["kind"] == "rest"
    assert ligne["tool"] == f"{alias.verbe} {alias.nouveau}"
    assert ligne["args"]["deprecated_name"] == alias.ancien
    assert "957" not in repr(ligne["tool"]) + repr(ligne["args"]), (
        "le chemin réel peut porter un identifiant : seul le gabarit est écrit")
    assert ligne["sub"] is None and ligne["org_id"] == 12, "ce qui est connu, sans plus"
