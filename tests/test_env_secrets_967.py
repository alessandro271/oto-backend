"""La liste des secrets (`oto_mcp/env_secrets.py`, #967) ne nomme que des variables de
l'inventaire, et ses requis sont ceux dont une instance ne peut se passer."""
from oto_mcp import env_inventory as inv, env_secrets


def test_chaque_secret_est_une_variable_inventoriee():
    assert env_secrets.SECRETS <= set(inv.par_nom())


def test_les_familles_secretes_existent():
    assert env_secrets.FAMILLES_SECRETES <= {f.nom for f in inv.FAMILLES_DYNAMIQUES}


def test_hors_serveur_est_un_sous_ensemble_des_secrets():
    assert env_secrets.HORS_SERVEUR <= env_secrets.SECRETS


def test_les_requis_sont_les_secrets_de_classe_requise():
    requis = env_secrets.secrets_requis()
    assert {"DATABASE_URL", "OTO_MCP_MASTER_KEY"} <= set(requis)
    assert all(inv.par_nom()[n].classe is inv.Classe.REQUISE for n in requis)
    assert not set(requis) & env_secrets.HORS_SERVEUR


def test_la_moitie_secrete_d_un_credential_d_annuaire_est_un_secret():
    assert env_secrets.est_secret("OTO_MCP_LOGTO_M2M_SECRET")
    assert not env_secrets.est_secret("OTO_MCP_LOGTO_M2M_ID")
    assert not env_secrets.est_secret("OTO_ENV")
