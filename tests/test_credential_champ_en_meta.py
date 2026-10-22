"""Un champ de credential rangé dans `meta` (`in_meta`) : ajouté SANS changer la forme du chiffré.

Cas fondateur, 14/09/2026 : une clé Anthropic d'ORGANISATION fait refuser chaque requête
qui ne nomme pas son workspace. Ajouter `workspace_id` comme second champ ordinaire aurait
fait passer le chiffré d'une clé BRUTE à un JSON : les clés déjà déposées ne se seraient
plus relues, sans ordre de reconditionnement sûr sur une base partagée prod/préprod, et la
lecture tolérante des deux formes est précisément le repli retiré du coffre le 27/08.

Ces bancs tiennent :
- la garde d'import — `in_meta` sur un champ secret lève (`meta` sort en clair) ;
- la relecture À L'OCTET d'une clé déjà déposée, et une pose qui écrit encore une clé
  brute, jamais le workspace à sa place ;
- la pose partagée : le workspace part dans le patch de `meta`, se conserve à une repose
  de la seule clé, s'efface envoyé vide ; la capacité d'org l'écrit bien dans `meta` ;
- rien ne change pour un connecteur sans champ `in_meta`.
"""
from __future__ import annotations

import httpx
import pytest

from oto_mcp import anthropic_cle
from oto_mcp import credentials_store as CS
from oto_mcp import providers
from oto_mcp.providers._model import CredentialField


@pytest.fixture(autouse=True)
def _anthropic_accepte_la_cle(monkeypatch):
    """La pose essaie la clé chez Anthropic (`anthropic_cle`) : ici Anthropic l'accepte,
    sans réseau. Ces bancs portent sur la FORME du coffre ; la vérification a les siens
    (`test_cle_anthropic_verifiee_a_la_pose.py`)."""
    monkeypatch.setattr(anthropic_cle, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))))


def test_in_meta_sur_un_champ_secret_leve_a_l_import():
    with pytest.raises(TypeError) as e:
        CredentialField("jeton", "Jeton", secret=True, in_meta=True)
    assert "in_meta" in str(e.value) and "secret=False" in str(e.value)
    CredentialField("region", "Région", secret=False, in_meta=True)


def test_aucun_champ_en_meta_du_registre_n_est_secret():
    fautifs = [(c.name, f.name) for c in providers.REGISTRY.values()
               for f in c.secret_fields if f.in_meta and f.secret]
    assert fautifs == []


def test_anthropic_garde_un_chiffre_a_un_seul_champ():
    c = providers.connector_for_provider("anthropic")
    assert [f.name for f in c.vault_fields] == ["key"]
    assert [f.name for f in c.secret_fields] == ["key", "workspace_id"]


def test_une_cle_anthropic_deja_deposee_se_relit_a_l_octet():
    brut = "sk-ant-api03-deja-deposee"
    assert CS.unpack_secret("anthropic", brut) == {"key": brut}


@pytest.mark.parametrize("champs", [{"key": "sk-ant-api03-neuve", "workspace_id": "wrkspc_1"},
                                    {"workspace_id": "wrkspc_1", "key": "sk-ant-api03-neuve"}])
def test_la_pose_ecrit_la_cle_brute_jamais_le_workspace(champs):
    assert CS.pack_secret("anthropic", champs) == "sk-ant-api03-neuve"


@pytest.fixture
def ligne(monkeypatch):
    """La ligne du coffre que l'écriture partielle relit (`None` = rien de posé)."""
    etat = {"ligne": None}
    monkeypatch.setattr(CS, "get_credential_with_meta", lambda *a, **k: etat["ligne"])
    return etat


def test_la_pose_range_le_workspace_dans_meta(ligne):
    assert CS.preparer_pose("org", "42", "anthropic", "",
                            fields={"key": "sk-ant-api03-neuve", "workspace_id": "wrkspc_1"}) == (
        "sk-ant-api03-neuve", {"workspace_id": "wrkspc_1"})


def test_une_cle_posee_seule_par_api_key_garde_le_workspace(ligne):
    """La forme d'avant l'ajout du champ, que des fronts envoient encore."""
    ligne["ligne"] = {"secret": "sk-ant-api03-ancienne",
                      "meta": {"workspace_id": "wrkspc_1", "verified_at": "2026-09-14"}}
    assert CS.preparer_pose("org", "42", "anthropic", "", api_key="sk-ant-api03-neuve") == (
        "sk-ant-api03-neuve", {"workspace_id": "wrkspc_1"})


def test_une_repose_du_seul_workspace_garde_la_cle(ligne):
    ligne["ligne"] = {"secret": "sk-ant-api03-ancienne", "meta": {"workspace_id": "wrkspc_1"}}
    assert CS.preparer_pose("org", "42", "anthropic", "",
                            fields={"workspace_id": "wrkspc_2"}) == (
        "sk-ant-api03-ancienne", {"workspace_id": "wrkspc_2"})


def test_un_workspace_envoye_vide_s_efface(ligne):
    ligne["ligne"] = {"secret": "sk-ant-api03-ancienne", "meta": {"workspace_id": "wrkspc_1"}}
    assert CS.preparer_pose("org", "42", "anthropic", "", fields={"workspace_id": ""}) == (
        "sk-ant-api03-ancienne", {})


def test_un_workspace_sans_cle_est_refuse_et_nomme(ligne):
    with pytest.raises(CS.CredentialFieldsInvalid) as e:
        CS.preparer_pose("org", "42", "anthropic", "", fields={"workspace_id": "wrkspc_1"})
    assert e.value.code == "missing_credentials"


def test_un_connecteur_sans_champ_en_meta_ne_change_pas(ligne):
    """`mistral` : une clé brute posée par `api_key`, sans relire le coffre, patch vide."""
    ligne["ligne"] = {"secret": "jamais-relue", "meta": {}}
    assert CS.preparer_pose("org", "42", "mistral", "", api_key="cle-mistral") == (
        "cle-mistral", {})


def test_la_capacite_d_org_ecrit_le_workspace_dans_meta(ligne, monkeypatch):
    from oto_mcp import org_store
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import secrets as S
    ecrit = {}
    monkeypatch.setattr(org_store, "get_org", lambda org_id: {"id": org_id})
    monkeypatch.setattr(CS, "guard_account_write", lambda *a, **k: None)
    monkeypatch.setattr(org_store, "set_org_secret",
                        lambda org_id, provider, secret, set_by=None, meta=None, account="":
                        ecrit.update(secret=secret, meta=meta))
    S._set_secret(ResolvedCtx(sub="membre", org_id=42), S.SetSecretInput(
        org_id=42, provider="anthropic", fields={"key": "sk-ant-api03-neuve", "workspace_id": "wrkspc_1"}))
    assert ecrit == {"secret": "sk-ant-api03-neuve", "meta": {"workspace_id": "wrkspc_1"}}


# ── La forme de la clé, vérifiée au dépôt ────────────────────────────────────

@pytest.mark.parametrize("cle", ["sk-ant-api03-neuve", "sk-ant-usr-neuve", "sk-ant-svc-neuve"])
def test_les_trois_formes_de_cle_console_se_deposent(ligne, cle):
    assert CS.preparer_pose("org", "42", "anthropic", "", fields={"key": cle}) == (cle, {})


@pytest.mark.parametrize("cle", ["sk-ant-oat01-jeton-de-session", "sk-ant-admin01-cle-d-admin",
                                 "sk-proj-une-autre-cle", "cle-quelconque"])
def test_une_cle_d_une_autre_forme_est_refusee_sans_etre_repetee(ligne, cle):
    """Un jeton de session ou une clé d'admin s'écrivait `ok: true` et n'échouait qu'à la
    réservation, chez le worker. Le refus nomme la forme attendue, jamais la valeur."""
    for pose in ({"fields": {"key": cle, "workspace_id": "wrkspc_1"}}, {"api_key": cle}):
        with pytest.raises(CS.CredentialFieldsInvalid) as e:
            CS.preparer_pose("org", "42", "anthropic", "", **pose)
        assert e.value.code == "invalid_field_value"
        assert "sk-ant-api…" in e.value.message and "sk-ant-usr…" in e.value.message
        assert cle not in e.value.message and cle not in str(e.value)


def test_la_capacite_d_org_refuse_une_cle_d_une_autre_forme(ligne, monkeypatch):
    from oto_mcp import org_store
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    from oto_mcp.capabilities.orgs import secrets as S
    monkeypatch.setattr(org_store, "get_org", lambda org_id: {"id": org_id})
    monkeypatch.setattr(org_store, "set_org_secret",
                        lambda *a, **k: pytest.fail("une clé refusée a été écrite"))
    with pytest.raises(AuthzDenied) as e:
        S._set_secret(ResolvedCtx(sub="membre", org_id=42), S.SetSecretInput(
            org_id=42, provider="anthropic", api_key="sk-ant-oat01-jeton"))
    assert e.value.code == "invalid_field_value"
    assert "sk-ant-oat01-jeton" not in str(e.value.message)
