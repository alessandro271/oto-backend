"""La clé `anthropic` essayée CHEZ ANTHROPIC à la pose (`oto_mcp/anthropic_cle.py`).

La forme de la clé (`prefixes`) ne dit pas si elle marche. Une clé d'ORGANISATION déposée
sans son workspace passait la pose, puis chaque requête du worker recevait un 400 : la
pose dit « écrit », le travail échoue plus tard, ailleurs, devant personne.

Ces bancs tiennent, SANS aucun appel réel (transport bouchon `httpx.MockTransport`) :
- 200 : la pose se fait, par un `GET /v1/models?limit=1` qui porte la clé et la version ;
- clé d'organisation sans workspace (le 400 tel que la doc d'Anthropic le décrit) :
  refus, avec la phrase qui dit quoi faire ;
- la même clé avec son workspace : l'en-tête part, la pose se fait ;
- 401 / 403 / workspace inconnu : refus avec la raison ;
- réseau, délai, 5xx : refus, AUCUN repli ;
- la clé n'apparaît dans aucun message ni journal, le workspace dans aucun journal ;
- les quatre chemins de pose partagée (org, équipe, tenant, plateforme) y passent, et
  une pose refusée n'écrit rien.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from oto_mcp import anthropic_cle
from oto_mcp import credentials_store as CS

CLE = "sk-ant-api03-cle-du-banc-0123456789"
WORKSPACE = "wrkspc_01BancBancBancBancBancBanc"

#: Le corps du 400 d'une clé non rattachée à un workspace, tel que la documentation
#: d'Anthropic le donne (Authentication › Select a workspace).
_CORPS_SANS_WORKSPACE = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": ("This API key is not scoped to a workspace, so this request must "
                    "include the anthropic-workspace-id header with the ID of the "
                    "workspace to use. Add the header, or use an API key that is scoped "
                    "to a workspace."),
    },
    "request_id": "req_banc",
}


def _erreur(type_: str, message: str) -> dict:
    return {"type": "error", "error": {"type": type_, "message": message},
            "request_id": "req_banc"}


@pytest.fixture
def anthropic(monkeypatch):
    """Anthropic bouchonné : `etat["repondre"](requete) -> httpx.Response` (ou lève),
    chaque requête reçue est gardée dans `etat["requetes"]`."""
    etat = {"requetes": [], "repondre": lambda r: httpx.Response(200, json={"data": []})}

    def _transport(requete: httpx.Request) -> httpx.Response:
        etat["requetes"].append(requete)
        return etat["repondre"](requete)

    monkeypatch.setattr(anthropic_cle, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(_transport), timeout=anthropic_cle.DELAI_S))
    return etat


@pytest.fixture
def coffre_vide(monkeypatch):
    """Rien de posé : l'écriture partielle (#448) ne relit rien."""
    monkeypatch.setattr(CS, "get_credential_with_meta", lambda *a, **k: None)


def _org_sans_workspace(requete: httpx.Request) -> httpx.Response:
    """Une clé d'ORGANISATION : refusée sans l'en-tête, acceptée avec."""
    if requete.headers.get("anthropic-workspace-id") == WORKSPACE:
        return httpx.Response(200, json={"data": []})
    return httpx.Response(400, json=_CORPS_SANS_WORKSPACE)


def _poser(**champs):
    return CS.preparer_pose("org", "42", "anthropic", "", fields=champs)


def _sans_secret(texte: str) -> None:
    assert CLE not in texte


# ── le comportement, cas par cas ─────────────────────────────────────────────

def test_200_la_pose_se_fait_par_un_appel_leger(anthropic, coffre_vide):
    assert _poser(key=CLE) == (CLE, {})
    (r,) = anthropic["requetes"]
    assert r.method == "GET"
    assert str(r.url) == "https://api.anthropic.com/v1/models?limit=1"
    assert r.headers["x-api-key"] == CLE
    assert r.headers["anthropic-version"] == "2023-06-01"
    assert "anthropic-workspace-id" not in r.headers


def test_cle_d_organisation_sans_workspace_refusee_avec_la_consigne(anthropic, coffre_vide):
    anthropic["repondre"] = _org_sans_workspace
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key=CLE)
    assert e.value.code == "anthropic_workspace_required"
    assert e.value.message == ("clé d'organisation Anthropic : renseigner le champ "
                               "Workspace, ou déposer une clé créée dans un workspace")
    _sans_secret(e.value.message)
    _sans_secret(str(e.value))


def test_la_meme_cle_avec_son_workspace_envoie_l_entete_et_se_pose(anthropic, coffre_vide):
    anthropic["repondre"] = _org_sans_workspace
    assert _poser(key=CLE, workspace_id=WORKSPACE) == (CLE, {"workspace_id": WORKSPACE})
    (r,) = anthropic["requetes"]
    assert r.headers["anthropic-workspace-id"] == WORKSPACE


def test_une_repose_du_seul_workspace_verifie_la_cle_deja_deposee(anthropic, monkeypatch):
    """L'écriture partielle complète la saisie par le coffre : c'est la PAIRE qui part."""
    monkeypatch.setattr(CS, "get_credential_with_meta",
                        lambda *a, **k: {"secret": CLE, "meta": {}})
    anthropic["repondre"] = _org_sans_workspace
    assert _poser(workspace_id=WORKSPACE) == (CLE, {"workspace_id": WORKSPACE})
    (r,) = anthropic["requetes"]
    assert r.headers["x-api-key"] == CLE
    assert r.headers["anthropic-workspace-id"] == WORKSPACE


@pytest.mark.parametrize("statut, type_, attendu", [
    (401, "authentication_error", "clé invalide, révoquée ou expirée"),
    (403, "permission_error", "sans droit"),
])
def test_cle_refusee_par_anthropic(anthropic, coffre_vide, statut, type_, attendu):
    anthropic["repondre"] = lambda r: httpx.Response(
        statut, json=_erreur(type_, f"invalid x-api-key {CLE}"))
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key=CLE)
    assert e.value.code == "anthropic_key_rejected"
    assert f"HTTP {statut}" in e.value.message and attendu in e.value.message
    _sans_secret(e.value.message)


def test_workspace_inconnu_refuse_sans_le_repeter(anthropic, coffre_vide):
    anthropic["repondre"] = lambda r: httpx.Response(
        404, json=_erreur("not_found_error", f"Workspace `{WORKSPACE}` not found."))
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key=CLE, workspace_id=WORKSPACE)
    assert e.value.code == "anthropic_key_rejected"
    assert "Workspace introuvable" in e.value.message
    assert WORKSPACE not in e.value.message


def test_un_autre_400_est_relaye_et_n_est_pas_lu_comme_une_cle_d_organisation(
        anthropic, coffre_vide):
    anthropic["repondre"] = lambda r: httpx.Response(
        400, json=_erreur("invalid_request_error", "spend limit reached"))
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key=CLE)
    assert e.value.code == "anthropic_key_rejected"
    assert "HTTP 400" in e.value.message and "spend limit reached" in e.value.message


def _leve(exc):
    def _r(requete):
        raise exc
    return _r


@pytest.mark.parametrize("repondre, nomme", [
    (_leve(httpx.ConnectError("connexion refusée")), "ConnectError"),
    (_leve(httpx.ReadTimeout("délai dépassé")), "ReadTimeout"),
    (lambda r: httpx.Response(503, json=_erreur("api_error", "indisponible")), "HTTP 503"),
    (lambda r: httpx.Response(529, json=_erreur("overloaded_error", "surchargé")), "HTTP 529"),
])
def test_anthropic_injoignable_refuse_sans_repli(anthropic, coffre_vide, repondre, nomme):
    anthropic["repondre"] = repondre
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key=CLE)
    assert e.value.code == "anthropic_unreachable"
    assert nomme in e.value.message and "pas déposée" in e.value.message
    _sans_secret(e.value.message)


def test_le_delai_de_l_appel_est_borne():
    with anthropic_cle._client() as client:
        assert client.timeout.read == anthropic_cle.DELAI_S == 10.0


def test_une_forme_refusee_ne_part_pas_chez_anthropic(anthropic, coffre_vide):
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        _poser(key="sk-ant-oat01-jeton-de-session")
    assert e.value.code == "invalid_field_value"
    assert anthropic["requetes"] == []


def test_un_autre_connecteur_n_appelle_pas_anthropic(anthropic, coffre_vide):
    assert CS.preparer_pose("org", "42", "mistral", "", api_key="cle-mistral") == (
        "cle-mistral", {})
    assert anthropic["requetes"] == []


# ── ni la clé ni le workspace dans un journal ────────────────────────────────

@pytest.mark.parametrize("repondre", [
    lambda r: httpx.Response(200, json={"data": []}),
    _org_sans_workspace,
    lambda r: httpx.Response(401, json=_erreur("authentication_error", "invalid x-api-key")),
    _leve(httpx.ConnectError("connexion refusée")),
    lambda r: httpx.Response(503, json=_erreur("api_error", "indisponible")),
])
@pytest.mark.parametrize("workspace", [None, WORKSPACE])
def test_ni_la_cle_ni_le_workspace_dans_les_journaux(anthropic, coffre_vide, caplog,
                                                     repondre, workspace):
    anthropic["repondre"] = repondre
    caplog.set_level(logging.DEBUG)
    champs = {"key": CLE, **({"workspace_id": workspace} if workspace else {})}
    try:
        _poser(**champs)
    except CS.CredentialFieldsInvalid as e:
        _sans_secret(e.message)
        _sans_secret(str(e))
        assert e.__cause__ is None
    assert anthropic["requetes"], "l'appel doit avoir eu lieu, sinon le banc ne prouve rien"
    assert CLE not in caplog.text
    assert WORKSPACE not in caplog.text


# ── les quatre chemins de pose partagée passent par la vérification ──────────

def _pose_org(monkeypatch, ecrit):
    from oto_mcp import org_store
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import secrets as S
    monkeypatch.setattr(org_store, "get_org", lambda org_id: {"id": org_id})
    monkeypatch.setattr(org_store, "set_org_secret",
                        lambda *a, **k: ecrit.append(("org", a, k)))
    return lambda champs: S._set_secret(ResolvedCtx(sub="admin", org_id=42),
                                        S.SetSecretInput(org_id=42, provider="anthropic",
                                                         fields=champs))


def _pose_equipe(monkeypatch, ecrit):
    from oto_mcp import group_store
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.groups import secrets as S
    monkeypatch.setattr(group_store, "set_group_secret",
                        lambda *a, **k: ecrit.append(("group", a, k)))
    return lambda champs: S._set_secret(ResolvedCtx(sub="chef", org_id=42),
                                        S.SetGroupSecretInput(group_id=7, provider="anthropic",
                                                              fields=champs))


def _pose_tenant(monkeypatch, ecrit):
    from oto_mcp import db, tenancy, tenant_vault
    from oto_mcp.capabilities import tenant_keys as T
    from oto_mcp.capabilities._types import ResolvedCtx
    monkeypatch.setattr(db, "tenant_exists", lambda slug: True)
    monkeypatch.setattr(tenancy, "primary_slug", lambda: "primaire")
    monkeypatch.setattr(tenant_vault, "set_tenant_secret",
                        lambda *a, **k: ecrit.append(("tenant", a, k)))
    return lambda champs: T._set_key(ResolvedCtx(sub="admin", org_id=None),
                                     T.TenantKeySetInput(slug="un-tenant", provider="anthropic",
                                                         fields=champs))


def _pose_plateforme(monkeypatch, ecrit):
    from oto_mcp.capabilities import api_tokens as A
    from oto_mcp.capabilities._types import ResolvedCtx
    monkeypatch.setattr(CS, "set_platform_key",
                        lambda *a, **k: ecrit.append(("platform", a, k)))
    return lambda champs: A._keys_create(ResolvedCtx(sub="admin", org_id=None),
                                         A.PlatformKeyCreateInput(provider="anthropic",
                                                                  label="partagee",
                                                                  fields=champs))


_CHEMINS = [_pose_org, _pose_equipe, _pose_tenant, _pose_plateforme]


@pytest.fixture
def sans_garde_de_compte(monkeypatch):
    monkeypatch.setattr(CS, "guard_account_write", lambda *a, **k: None)


@pytest.mark.parametrize("chemin", _CHEMINS, ids=lambda f: f.__name__)
def test_chaque_chemin_refuse_une_cle_d_organisation_sans_workspace_et_n_ecrit_rien(
        anthropic, coffre_vide, sans_garde_de_compte, monkeypatch, chemin):
    from oto_mcp.capabilities._types import AuthzDenied
    ecrit: list = []
    poser = chemin(monkeypatch, ecrit)
    anthropic["repondre"] = _org_sans_workspace
    with pytest.raises(AuthzDenied) as e:
        poser({"key": CLE})
    assert e.value.status == 400
    assert e.value.code == "anthropic_workspace_required"
    assert "renseigner le champ Workspace" in str(e.value.message)
    _sans_secret(str(e.value.message))
    assert ecrit == [], "une clé refusée a été écrite"


@pytest.mark.parametrize("chemin", _CHEMINS, ids=lambda f: f.__name__)
def test_chaque_chemin_refuse_quand_anthropic_est_injoignable(
        anthropic, coffre_vide, sans_garde_de_compte, monkeypatch, chemin):
    from oto_mcp.capabilities._types import AuthzDenied
    ecrit: list = []
    poser = chemin(monkeypatch, ecrit)
    anthropic["repondre"] = _leve(httpx.ConnectTimeout("délai de connexion"))
    with pytest.raises(AuthzDenied) as e:
        poser({"key": CLE, "workspace_id": WORKSPACE})
    assert e.value.code == "anthropic_unreachable"
    assert ecrit == [], "une clé non vérifiée a été écrite"


@pytest.mark.parametrize("chemin", _CHEMINS, ids=lambda f: f.__name__)
def test_chaque_chemin_pose_la_cle_avec_son_workspace(
        anthropic, coffre_vide, sans_garde_de_compte, monkeypatch, chemin):
    ecrit: list = []
    poser = chemin(monkeypatch, ecrit)
    anthropic["repondre"] = _org_sans_workspace
    poser({"key": CLE, "workspace_id": WORKSPACE})
    assert len(ecrit) == 1
    (r,) = anthropic["requetes"]
    assert r.headers["anthropic-workspace-id"] == WORKSPACE
