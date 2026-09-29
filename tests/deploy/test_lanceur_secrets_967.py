"""Le lanceur générique (`deploy/lanceur_secrets.py`, #967) : ce qu'il tire se dérive de
l'inventaire, où il le tire se déclare, et chaque absence refuse en nommant."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import pathlib
import urllib.error
import urllib.parse

import pytest

from oto_mcp import env_secrets

_CHEMIN = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "lanceur_secrets.py"
_spec = importlib.util.spec_from_file_location("lanceur_secrets", _CHEMIN)
lanceur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lanceur)


def _env(tmp_path, **plus):
    (tmp_path / "scw").write_text("cle-api\n")
    env = {"OTO_SECRETS_REGION": "fr-par", "OTO_SECRETS_PROJET": "projet-1",
           "OTO_SECRETS_CHEMIN": "/prod", "OTO_SECRETS_OPTIONNELS": "",
           "CREDENTIALS_DIRECTORY": str(tmp_path), "OTO_ENV": "prod"}
    env.update(plus)
    return env


class _Reponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _gestionnaire(valeurs: dict, appels: list):
    def ouvrir(requete, timeout):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(requete.full_url).query)
        appels.append((requete.full_url.split("?")[0], q, requete.headers, timeout))
        nom = q["secret_name"][0]
        if nom not in valeurs:
            raise urllib.error.HTTPError(requete.full_url, 404, "not found", {}, None)
        corps = {"data": base64.b64encode(valeurs[nom].encode()).decode()}
        return _Reponse(json.dumps(corps).encode())
    return ouvrir


def _tous_requis(suffixe="\n"):
    return {n: f"valeur-{n}{suffixe}" for n in env_secrets.secrets_requis()}


def test_tire_les_requis_par_nom_dans_le_projet_et_le_chemin_de_l_instance(tmp_path):
    appels = []
    env, noms = lanceur.preparer(_env(tmp_path), _gestionnaire(_tous_requis(), appels))
    assert noms == list(env_secrets.secrets_requis())
    for nom in noms:
        assert env[nom] == f"valeur-{nom}"          # fin de ligne retirée
    url, q, entetes, delai = appels[0]
    assert url == ("https://api.scaleway.com/secret-manager/v1beta1/regions/fr-par"
                   "/secrets-by-path/versions/latest_enabled/access")
    assert q["project_id"] == ["projet-1"] and q["secret_path"] == ["/prod"]
    assert entetes["X-auth-token"] == "cle-api" and delai == lanceur.DELAI_S


def test_un_facultatif_declare_est_tire(tmp_path):
    valeurs = {**_tous_requis(), "MISTRAL_API_KEY": "m"}
    env, noms = lanceur.preparer(_env(tmp_path, OTO_SECRETS_OPTIONNELS="MISTRAL_API_KEY"),
                                 _gestionnaire(valeurs, []))
    assert noms[-1] == "MISTRAL_API_KEY" and env["MISTRAL_API_KEY"] == "m"


def test_un_secret_introuvable_refuse_en_le_nommant(tmp_path):
    valeurs = _tous_requis()
    del valeurs["OTO_MCP_MASTER_KEY"]
    with pytest.raises(lanceur.Refus, match=r"OTO_MCP_MASTER_KEY introuvable dans /prod \(HTTP 404\)"):
        lanceur.preparer(_env(tmp_path), _gestionnaire(valeurs, []))


def test_un_facultatif_declare_mais_absent_refuse(tmp_path):
    with pytest.raises(lanceur.Refus, match="MOLLIE_API_KEY introuvable"):
        lanceur.preparer(_env(tmp_path, OTO_SECRETS_OPTIONNELS="MOLLIE_API_KEY"),
                         _gestionnaire(_tous_requis(), []))


def test_un_secret_vide_refuse(tmp_path):
    valeurs = {**_tous_requis(), "DATABASE_URL": "\n"}
    with pytest.raises(lanceur.Refus, match="DATABASE_URL est vide"):
        lanceur.preparer(_env(tmp_path), _gestionnaire(valeurs, []))


@pytest.mark.parametrize("nom, motif", [
    ("OTO_ENV", "n'est pas un secret"),
    ("OTO_EXPORT_CLE_CIBLE", "jamais dans l'environnement du serveur"),
    ("DATABASE_URL", "est requis"),
])
def test_un_facultatif_mal_declare_refuse(tmp_path, nom, motif):
    with pytest.raises(lanceur.Refus, match=motif):
        lanceur.preparer(_env(tmp_path, OTO_SECRETS_OPTIONNELS=nom), _gestionnaire({}, []))


def test_les_facultatifs_doivent_etre_declares_meme_vides(tmp_path):
    env = _env(tmp_path)
    del env["OTO_SECRETS_OPTIONNELS"]
    with pytest.raises(lanceur.Refus, match="OTO_SECRETS_OPTIONNELS n'est pas déclarée"):
        lanceur.preparer(env, _gestionnaire({}, []))


def test_un_secret_venu_du_env_refuse(tmp_path):
    with pytest.raises(lanceur.Refus, match="déjà dans l'environnement.*MOLLIE_API_KEY"):
        lanceur.preparer(_env(tmp_path, MOLLIE_API_KEY="x"), _gestionnaire({}, []))


@pytest.mark.parametrize("retire", ["OTO_SECRETS_REGION", "OTO_SECRETS_PROJET",
                                    "OTO_SECRETS_CHEMIN"])
def test_la_configuration_absente_refuse(tmp_path, retire):
    env = _env(tmp_path)
    del env[retire]
    with pytest.raises(lanceur.Refus, match=retire):
        lanceur.preparer(env, _gestionnaire({}, []))


def test_sans_cle_d_api_refuse(tmp_path):
    env = _env(tmp_path)
    del env["CREDENTIALS_DIRECTORY"]
    with pytest.raises(lanceur.Refus, match="LoadCredential"):
        lanceur.preparer(env, _gestionnaire({}, []))


def test_main_execute_le_serveur_de_son_arbre_sans_ecrire_une_valeur(tmp_path, monkeypatch, capsys):
    for n, v in _env(tmp_path).items():
        monkeypatch.setenv(n, v)
    monkeypatch.setattr(lanceur, "preparer",
                        lambda env: ({**env, "DATABASE_URL": "postgres://secret"}, ["DATABASE_URL"]))
    execute = {}
    monkeypatch.setattr(lanceur.os, "execve",
                        lambda chemin, args, env: execute.update(chemin=chemin, args=args, env=env))
    lanceur.main(["lanceur_secrets.py", "maintenance", "all"])
    serveur = str(_CHEMIN.parents[1] / ".venv" / "bin" / "oto-mcp")
    assert execute["chemin"] == serveur
    assert execute["args"] == [serveur, "maintenance", "all"]
    assert execute["env"]["DATABASE_URL"] == "postgres://secret"
    assert "postgres://secret" not in capsys.readouterr().err


def test_main_rend_1_et_nomme_le_refus(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OTO_SECRETS_OPTIONNELS", raising=False)
    assert lanceur.main(["lanceur_secrets.py"]) == 1
    assert "lanceur : REFUS" in capsys.readouterr().err
