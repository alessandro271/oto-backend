"""La règle COMMUNE du compte prêté (`access/prets.py`) — indépendante de tout connecteur.

Un compte prêté par une autre personne ne sert que si `_account` le nomme ; sans nom et
sans rien d'autre, refus `account_required` qui liste les prêts et leur prêteur. Les
comptes propres, et ceux de l'org ou de l'équipe ELLES-MÊMES, servent sans nom.
"""
import pytest

from oto_mcp import access, db
from oto_mcp.mcp_errors import McpError

PROPRE = {"account": "moi"}
DE_L_EQUIPE = {"account": "equipe"}                 # la clé de l'équipe elle-même
PRET = {"account": "jane", "lent_by": "preteuse"}
PRET_EQUIPE = {"account": "john", "lent_by": "preteur",
               "via_group": {"id": 49, "name": "Ventes"}}


@pytest.fixture(autouse=True)
def _annuaire(monkeypatch):
    monkeypatch.setattr(db, "emails_by_subs",
                        lambda subs: {"preteuse": "preteuse@contoso.example"})
    monkeypatch.setattr(db, "get_user", lambda sub: {"name": "Pierre"} if sub == "preteur" else None)


def test_sans_nom_un_compte_non_prete_laisse_la_resolution_ordinaire():
    assert access.pret_a_servir([PROPRE, PRET], None, quoi="Outlook") is None
    assert access.pret_a_servir([DE_L_EQUIPE, PRET], None, quoi="Outlook") is None


def test_sans_nom_et_seulement_des_prets_refus_qui_les_nomme():
    with pytest.raises(McpError) as e:
        access.pret_a_servir([PRET, PRET_EQUIPE], None, quoi="Outlook")
    err = e.value.error
    assert err.data["code"] == "account_required"
    assert "`jane` (lent by preteuse@contoso.example)" in err.message
    assert "`john` (lent by Pierre via the team Ventes)" in err.message
    assert err.data["lent_accounts"][1] == {"account": "john", "lent_by": "Pierre",
                                            "via_group": {"id": 49, "name": "Ventes"}}


def test_le_nom_designe_le_pret():
    assert access.pret_a_servir([PRET, PRET_EQUIPE], "john", quoi="Outlook") is PRET_EQUIPE


def test_un_compte_propre_du_meme_nom_l_emporte():
    assert access.pret_a_servir([{"account": "jane"}, PRET], "jane", quoi="Outlook") is None


def test_un_nom_inconnu_ne_sert_aucun_pret():
    assert access.pret_a_servir([PRET], "inconnu", quoi="Outlook") is None


def test_deux_preteurs_sous_le_meme_nom_renvoient_a_instance():
    autre = {"account": "jane", "lent_by": "preteur"}
    with pytest.raises(McpError) as e:
        access.pret_a_servir([PRET, autre], "jane", quoi="Outlook")
    assert e.value.error.data["code"] == "instance_required"


def test_aucun_candidat_rien_a_decider():
    assert access.pret_a_servir([], None, quoi="Outlook") is None


def test_un_preteur_sans_fiche_est_nomme_par_son_sub():
    assert access.libelles_preteurs(["fantome"]) == {"fantome": "fantome"}
