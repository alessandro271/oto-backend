"""La page partagée d'un tableau (`<slug>.share.<D>/data/<id>`) sert ce que sert la
lecture d'un agent (`data_rows`), ni plus ni moins.

Deux écarts constatés sur la page publique :

1. **La politique de rédaction de l'org** (`field_filters`, service `data`) — appliquée
   au résultat de `data_rows` par `FieldRedactionMiddleware` — n'était pas appliquée :
   la page lisait la base et rendait les cellules brutes à quiconque tient le lien.
2. **Les colonnes déclarées** se lisaient par `name` alors qu'une colonne se déclare par
   `key` : la page ne trouvait aucune colonne déclarée et retombait sur l'union des clés
   des lignes (ordre perdu, colonne déclarée vide absente).

La référence est la lecture servie à un agent : `DatastorePg._row_to_dict` (projection
de TOUTE ligne servie) puis `redaction.redact_payload` (rédaction de TOUT résultat
d'outil). La page appelle ces deux fonctions, aucune copie de leur règle.
"""
import pytest

from oto_mcp import db, org_store, redaction, share_ui
from oto_mcp import subdomain_project as sp

_ORG = 7
_PROJECT = {"id": 5, "name": "Projet démo", "brief_md": "", "owner_type": "org",
            "owner_id": _ORG, "mcp_access": "secret", "mcp_tools": [],
            "mcp_expose_datastore": True, "mcp_expose_docs": False}
_LINKS = [{"target_type": "tableau", "target_ref": "22", "label": "Prospects",
           "datastore": "prospects", "datastore_id": 22}]
# Un schéma tel que le datastore le stocke : chaque colonne se déclare par `key`.
_SCHEMA = {"fields": [{"key": "nom", "type": "text"},
                      {"key": "telephone", "type": "text"},
                      {"key": "ville", "type": "text"}]}
_TEL = "0611223344"
_POLITIQUE = {"data": {"rules": [{"fields": ["telephone"], "action": "drop"}]}}


def _ligne(row_id, data):
    return {"row_id": row_id, "created_at": "2026-10-01 10:00:00",
            "updated_at": "2026-10-01 10:00:00", "rev": 1, "data": data}


@pytest.fixture
def page(monkeypatch):
    monkeypatch.delenv("OTO_MCP_DEV_SUB", raising=False)
    monkeypatch.setattr(db, "org_tenant_slug", lambda org_id: "oto")
    monkeypatch.setattr(db, "list_project_links", lambda pid: list(_LINKS))
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: {"datastore": "prospects", "schema": _SCHEMA})
    monkeypatch.setattr(db, "datastore_count_rows", lambda rid: 1)
    monkeypatch.setattr(db, "datastore_list_rows", lambda rid, **kw: [
        _ligne("r1", {"nom": "Alice", "telephone": _TEL})])
    politiques = {_ORG: dict(_POLITIQUE)}
    monkeypatch.setattr(org_store, "get_org_field_filters",
                        lambda org_id: dict(politiques.get(org_id) or {}))
    # Le contexte que `HostDispatch` pose pour TOUTE requête d'un endpoint secret —
    # la face MCP comme la face web.
    tok = sp._CTX.set(sp.AnonContext(5, _ORG, frozenset(), datastore_exposed=True))
    try:
        yield politiques
    finally:
        sp._CTX.reset(tok)


# ── 1. La politique de rédaction de l'org ────────────────────────────────────

def test_la_page_applique_la_politique_de_redaction_de_l_org(page):
    html, status = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert status == 200 and "Alice" in html
    assert _TEL not in html, "la page partagée sert une colonne que l'org fait retirer"


def test_sans_politique_la_page_sert_la_colonne(page):
    page.clear()
    html, status = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert status == 200 and _TEL in html


def test_la_face_mcp_sans_login_applique_aussi_la_politique(page):
    """`data_rows` sur l'endpoint secret : sans `sub`, la politique se lit sur l'org
    PROPRIÉTAIRE du projet (`scope.current_org(None)`), comme le dit son seam — elle ne
    se lisait sur aucune, et la sortie passait en clair."""
    out = redaction.redact_payload("data", {"rows": [{"nom": "Alice", "telephone": _TEL}]})
    assert out is not redaction.PASSTHROUGH
    assert out["rows"] == [{"nom": "Alice"}]


def test_une_politique_illisible_retient_la_page(page, monkeypatch):
    """Fail-closed, comme `FieldRedactionMiddleware` : ne pas savoir ce que l'org
    retire n'autorise pas à tout servir."""
    def _panne(org_id):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(org_store, "get_org_field_filters", _panne)
    html, status = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert status == 503 and _TEL not in html and "Alice" not in html


def test_la_page_exige_le_contexte_de_son_endpoint(page):
    """Hors du contexte de l'endpoint (ou sous celui d'un autre projet), la page ne
    sait pas sous quelle org lire la politique : elle lève au lieu de servir en clair."""
    tok = sp._CTX.set(sp.AnonContext(6, _ORG, frozenset(), datastore_exposed=True))
    try:
        with pytest.raises(RuntimeError):
            share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    finally:
        sp._CTX.reset(tok)


@pytest.mark.asyncio
async def test_le_dispatch_pose_le_contexte_avant_la_page(monkeypatch):
    """La face web est rendue SOUS le contexte de l'endpoint, comme la face MCP."""
    sp._BUCKETS.clear()
    vu = {}

    async def _app(scope, receive, send):
        vu["app"] = True

    async def _send(msg):
        if msg["type"] == "http.response.start":
            vu["status"] = msg["status"]

    def _page(proj, path, **kw):
        vu["org"] = sp.current_anon_org()
        vu["projet"] = sp.current_anon_project_id()
        return "<html>UI</html>", 200

    monkeypatch.setattr(sp, "resolve_project", lambda host: dict(_PROJECT))
    monkeypatch.setattr(sp, "_org_suspendue", lambda org_id: False)
    monkeypatch.setattr(share_ui, "build_page", _page)
    scope = {"type": "http", "method": "GET", "path": "/data/22", "query_string": b"",
             "headers": [(b"host", b"p.share.oto.cx"), (b"accept", b"text/html")]}
    await sp.HostDispatch(_app, _app)(scope, None, _send)
    assert vu.get("status") == 200 and "app" not in vu
    assert vu.get("org") == _ORG and vu.get("projet") == 5
    assert sp.current_anon_context() is None


# ── 2. Les colonnes déclarées se lisent par `key` ────────────────────────────

def test_les_colonnes_declarees_sont_celles_du_schema(page):
    page.clear()
    html, status = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert status == 200
    # `ville` est déclarée et vide : la lecture d'agent la sert à `null`, la page
    # l'affiche comme colonne ; l'ordre est celui du schéma.
    entetes = [c for c in ("nom", "telephone", "ville") if f"<span>{c}</span>" in html]
    assert entetes == ["nom", "telephone", "ville"]
    assert html.index("<span>nom</span>") < html.index("<span>telephone</span>") \
        < html.index("<span>ville</span>")


def test_une_colonne_retiree_par_la_politique_n_a_pas_d_entete(page):
    html, _ = share_ui.build_page(_PROJECT, "/data/22", connect_url="u")
    assert "<span>telephone</span>" not in html
    assert "<span>nom</span>" in html and "<span>ville</span>" in html
