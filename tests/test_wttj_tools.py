"""Tools `wttj_*` — dispatch `op=`, projection des listes, sonde de connexion.

Ce que ce fichier verrouille : chaque op appelle la BONNE méthode du client (une op
mal câblée, dans un ATS, crée ou déplace un candidat réel), le défaut d'`op` est une
lecture, un argument obligatoire manquant ou un argument hors de son op est refusé
AVANT tout appel, une liste rend sa vue de tri (les corps deviennent leur taille), et
un CV rendu porte son lien DURABLE (`resume_link`, scellé compte / org / équipe /
candidat) à côté du lien de WTTJ qui expire. La route qui l'ouvre :
`tests/api/test_wttj_resume.py`.
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError

_WRITE_METHODS = ("create_candidate", "update_candidate", "create_comment")


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import wttj

    m = FastMCP("t")
    wttj.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client():
    """Faux WttjAtsClient + clé résolue (le module instancie le client à CHAQUE appel)."""
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
            patch("oto.tools.wttj_ats.WttjAtsClient") as cls:
        cls.return_value.list_jobs.return_value = []
        cls.return_value.list_candidates.return_value = []
        cls.return_value.list_moves.return_value = []
        yield cls.return_value


def _no_write(client):
    for m in _WRITE_METHODS:
        getattr(client, m).assert_not_called()


# --- organisations -------------------------------------------------------------

def test_organization_lit_les_organisations_du_jeton(client):
    _tool("wttj_organization")()
    client.get_current_user.assert_called_once_with(organizations=True)
    client.get_organization.assert_not_called()


def test_organization_n_a_plus_de_detail():
    """Le détail d'une organisation exige un scope de partenaire qu'un compte client
    n'obtient pas : l'outil ne l'offre plus, il ne prend aucun paramètre."""
    import inspect

    assert inspect.signature(_tool("wttj_organization")).parameters == {}


# --- offres ----------------------------------------------------------------------

def test_job_list_exige_l_organisation_et_projette(client):
    with pytest.raises(McpError, match="organization_reference"):
        _tool("wttj_job")(op="list")
    client.list_jobs.return_value = [
        {"reference": "J1", "name": "Dev", "status": "published", "description": "x" * 40}]
    out = _tool("wttj_job")(organization_reference="org", status="published", page=2)
    assert client.list_jobs.call_args.args == ("org",)
    assert client.list_jobs.call_args.kwargs["status"] == "published"
    row = out["jobs"][0]
    assert "description" not in row and row["description_length"] == 40
    assert out["projection"]["omitted"] == ["description"]
    assert out["page"] == 2 and out["count"] == 1


def test_job_list_brut_sur_demande(client):
    client.list_jobs.return_value = [{"reference": "J1", "description": "x"}]
    out = _tool("wttj_job")(organization_reference="org", fields=["*"])
    assert out["jobs"][0]["description"] == "x" and "projection" not in out


def test_job_get_rend_les_etapes(client):
    _tool("wttj_job")(op="get", job_reference="J1")
    client.get_job.assert_called_once_with("J1", stages=True, candidates_count=None)


def test_job_get_refuse_un_filtre_de_liste(client):
    with pytest.raises(McpError, match="status"):
        _tool("wttj_job")(op="get", job_reference="J1", status="draft")
    client.get_job.assert_not_called()


# --- candidats -------------------------------------------------------------------

def test_candidate_list_est_le_defaut_et_n_ecrit_rien(client):
    client.list_candidates.return_value = [{"reference": "C1", "cover_letter": "abc"}]
    out = _tool("wttj_candidate")(job_reference="J1", job_stage_id=3)
    kw = client.list_candidates.call_args.kwargs
    assert client.list_candidates.call_args.args == ("J1",) and kw["job_stage_id"] == 3
    assert out["candidates"][0]["cover_letter_length"] == 3
    _no_write(client)


def test_candidate_list_exige_le_job(client):
    with pytest.raises(McpError, match="job_reference"):
        _tool("wttj_candidate")()
    client.list_candidates.assert_not_called()


def test_candidate_create_exige_ses_six_champs(client):
    with pytest.raises(McpError, match="job_stage_id"):
        _tool("wttj_candidate")(op="create", organization_reference="org",
                                job_reference="J1", email="a@b.c",
                                firstname="Ada", lastname="Lovelace")
    _no_write(client)


def test_candidate_create_ecrit_cela_seulement(client):
    _tool("wttj_candidate")(op="create", organization_reference="org",
                            job_reference="J1", job_stage_id=3, email="a@b.c",
                            firstname="Ada", lastname="Lovelace",
                            candidate={"phone": "06"})
    client.create_candidate.assert_called_once_with(
        "org", "J1", 3, "a@b.c", "Ada", "Lovelace", phone="06")
    client.update_candidate.assert_not_called()
    client.create_comment.assert_not_called()


def test_candidate_update_deplace_par_l_etape(client):
    _tool("wttj_candidate")(op="update", candidate_reference="C1", job_stage_id=7)
    client.update_candidate.assert_called_once_with("C1", job_stage_id=7)
    client.create_candidate.assert_not_called()


def test_candidate_update_sans_changement_refuse(client):
    with pytest.raises(McpError, match="requires at least one change"):
        _tool("wttj_candidate")(op="update", candidate_reference="C1")
    _no_write(client)


def test_candidate_op_inconnue_nomme_les_ops(client):
    with pytest.raises(McpError, match="'list', 'get', 'create' or 'update'"):
        _tool("wttj_candidate")(op="delete")
    _no_write(client)


# --- CV : le lien durable --------------------------------------------------------

@pytest.fixture
def appelant(monkeypatch):
    """L'identité de l'appel (compte, org, équipe) sous laquelle la clé est résolue."""
    from oto_mcp import access

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-banc-assez-long")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://oto.example.test")
    vus = []
    monkeypatch.setattr(access, "current_user_sub_or_raise",
                        lambda: vus.append("sub") or "u-recruteur")
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "current_group", lambda sub: 31)
    return vus


def _jeton(lien: str) -> dict:
    from oto_mcp.tools import wttj_resume

    assert lien.startswith("https://oto.example.test/api/wttj/resume/")
    return wttj_resume.verify(lien.rsplit("/", 1)[-1])


def test_candidate_get_ajoute_le_lien_durable_du_cv(client, appelant):
    client.get_candidate.return_value = {
        "reference": "C1", "resume_url": "https://cdn.example.test/cv.pdf?Expires=1"}
    out = _tool("wttj_candidate")(op="get", candidate_reference="C1")["candidate"]
    assert out["resume_url"] == "https://cdn.example.test/cv.pdf?Expires=1", (
        "le lien brut reste : un agent qui télécharge tout de suite en a besoin")
    payload = _jeton(out["resume_link"])
    assert (payload["sub"], payload["org"]) == ("u-recruteur", 7)
    assert payload["target"] == {"kind": "wttj_resume", "group": 31,
                                 "candidate_reference": "C1"}
    assert payload["exp"] - time.time() > 29 * 24 * 3600, "le lien vit 30 jours"
    _no_write(client)


def test_candidate_get_sans_cv_ne_frappe_rien(client, appelant):
    client.get_candidate.return_value = {"reference": "C1", "resume_url": None}
    out = _tool("wttj_candidate")(op="get", candidate_reference="C1")["candidate"]
    assert "resume_link" not in out
    assert appelant == [], "sans CV, aucune identité n'est lue"


def test_candidate_list_ajoute_le_lien_aux_seules_lignes_qui_ont_un_cv(client, appelant):
    client.list_candidates.return_value = [
        {"reference": "C1", "resume_url": "https://cdn.example.test/1.pdf"},
        {"reference": "C2", "resume_url": None}]
    rows = _tool("wttj_candidate")(job_reference="J1")["candidates"]
    assert _jeton(rows[0]["resume_link"])["target"]["candidate_reference"] == "C1"
    assert "resume_link" not in rows[1]
    assert appelant == ["sub"], "une seule lecture d'identité pour toute la page"


def test_la_description_dit_quel_lien_donner_a_un_humain():
    doc = _tool("wttj_candidate").__doc__
    assert "resume_link" in doc and "30 min" in doc


# --- commentaire, mouvements ---------------------------------------------------

def test_comment(client):
    _tool("wttj_comment")(candidate_reference="C1", content="appel ok")
    client.create_comment.assert_called_once_with("C1", "appel ok")


def test_emails_d_un_candidat_projette_le_corps(client):
    client.list_emails.return_value = [
        {"subject": "Entretien", "created_at": "2026-10-01", "body": "x" * 12}]
    out = _tool("wttj_emails")(candidate_reference="C1", per_page=5)
    client.list_emails.assert_called_once_with("C1", page=None, per_page=5)
    row = out["emails"][0]
    assert "body" not in row and row["body_length"] == 12
    assert out["projection"]["omitted"] == ["body"]
    _no_write(client)


def test_emails_bruts_sur_demande(client):
    client.list_emails.return_value = [{"subject": "s", "body": "corps"}]
    out = _tool("wttj_emails")(candidate_reference="C1", fields=["*"])
    assert out["emails"][0]["body"] == "corps" and "projection" not in out


def test_moves(client):
    out = _tool("wttj_moves")(organization_reference="org", job_reference="J1")
    client.list_moves.assert_called_once_with("org", job_reference="J1", page=None,
                                              per_page=None)
    assert out["moves"] == []


def test_moves_exige_le_job():
    """L'API refuse l'historique sans `job_reference` : l'outil l'exige à la signature."""
    import inspect

    param = inspect.signature(_tool("wttj_moves")).parameters["job_reference"]
    assert param.default is inspect.Parameter.empty


# --- refus amont ---------------------------------------------------------------

def test_scope_manquant_est_nomme(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.list_jobs.side_effect = UpstreamHTTPError(
        403, {"error": "invalid_scope", "error_description": "jobs_r"}, service="wttj")
    with pytest.raises(McpError, match="scope"):
        _tool("wttj_job")(organization_reference="org")


# --- sonde ---------------------------------------------------------------------

def test_sonde_lit_l_utilisateur_courant():
    from oto_mcp import credentials_store
    from oto_mcp.tools import wttj

    with patch("oto.tools.wttj_ats.WttjAtsClient") as cls:
        wttj._verify(credentials_store.unpack_secret("wttj", "tok"))
    cls.assert_called_once_with(api_key="tok")
    cls.return_value.get_current_user.assert_called_once_with()


def test_la_sonde_est_enregistree():
    from fastmcp import FastMCP
    from oto_mcp.connectors import verify as cv
    from oto_mcp.tools import wttj

    wttj.register(FastMCP("t"))
    assert cv.probe_for("wttj") is wttj._verify
    assert cv.couverture("wttj") == cv.AUTH
