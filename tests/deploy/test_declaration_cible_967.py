"""La déclaration d'une cible (`deploy/cible/declaration.py`, #967) : refusée en entier
au moindre écart, validée contre l'inventaire du tag, et source de TOUT ce qui se
dérive — aucun chemin écrit par root ne vient d'une valeur libre."""
from __future__ import annotations

import copy
import importlib.util
import json

import pytest

from _banc_cible import DECLARATION, DEPOT

_spec = importlib.util.spec_from_file_location(
    "declaration_cible", DEPOT / "deploy" / "cible" / "declaration.py")
decl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decl)


@pytest.fixture
def doc():
    return json.loads(DECLARATION.read_text())


def _refus(doc) -> list[str]:
    with pytest.raises(decl.Refus) as e:
        decl.valider(doc)
    return e.value.ecarts


def test_l_exemple_est_conforme(doc):
    assert decl.valider(copy.deepcopy(doc)) == doc


@pytest.mark.parametrize("modifier, attendu", [
    (lambda d: d.update(instance="Exemple!"), "instance :"),
    (lambda d: d.update(inconnue=1), "declaration.inconnue : clé inconnue"),
    (lambda d: d["secrets"].pop("projet"), "secrets.projet : manquante"),
    (lambda d: d["secrets"].update(projet="mon-projet"), "UUID"),
    (lambda d: d["roles"].update(staging={}), "roles.staging : rôle inconnu"),
    (lambda d: d["roles"]["prod"]["ports"].update(green=9205), "partagent un port"),
    (lambda d: d["roles"]["prod"]["ports"].update(blue=80), "ports.blue : entier entre 1024"),
    (lambda d: d["roles"]["prod"].update(hote_public="https://x"), "hote_public : nom d'hôte"),
    (lambda d: d["roles"]["prod"].update(ask="oui"), "ask : booléen"),
    (lambda d: d["roles"]["prod"].update(drain_max=0), "drain_max"),
    (lambda d: d["roles"]["prod"]["env"].update(OTO_MCP_MASTER_KEY="x"),
     "OTO_MCP_MASTER_KEY : c'est un secret"),
    (lambda d: d["roles"]["prod"]["env"].update(PORT="1"), "PORT : posée par l'unité"),
    (lambda d: d["roles"]["prod"]["env"].update(VARIABLE_INVENTEE="1"),
     "VARIABLE_INVENTEE : variable absente de l'inventaire"),
    (lambda d: d["roles"]["prod"]["env"].update(OTO_ENV="a\nb"), "chaîne sur une ligne"),
    (lambda d: d["roles"]["prod"]["env"].pop("OTO_BRAND_NAME"),
     "OTO_BRAND_NAME : exigée par l'inventaire"),
    (lambda d: d["roles"]["prod"].update(secrets_optionnels=["OTO_ENV"]),
     "OTO_ENV n'est pas un secret facultatif"),
    (lambda d: d["roles"]["prod"].update(secrets_optionnels=["DATABASE_URL"]),
     "DATABASE_URL n'est pas un secret facultatif"),
    (lambda d: d["roles"]["prod"].update(secrets_optionnels=["OTO_EXPORT_CLE_CIBLE"]),
     "OTO_EXPORT_CLE_CIBLE n'est pas un secret facultatif"),
])
def test_chaque_ecart_est_nomme(doc, modifier, attendu):
    modifier(doc)
    assert any(attendu in e for e in _refus(doc)), _refus(doc)


def test_tous_les_ecarts_sont_nommes_d_un_coup(doc):
    doc["instance"] = "X"
    doc["roles"]["prod"]["env"]["DATABASE_URL"] = "x"
    assert len(_refus(doc)) == 2


def test_tout_se_derive_du_nom_et_du_role(doc):
    v = decl.variables(doc, "preprod")
    assert v["UTILISATEUR"] == "oto-exemple"
    assert v["BG_TREE"] == "/opt/exemple/preprod"
    assert v["BG_UPSTREAM"] == "/etc/caddy/upstream-exemple-preprod.conf"
    assert v["BG_ACTIVE"] == "/etc/exemple/preprod/active"
    assert v["BG_PUBLIC"] == "https://mcp-preprod.exemple.test/.well-known/oauth-authorization-server"
    assert v["BG_ASK"] == "" and v["BG_LANCEUR"] == "versionne"
    assert (v["BG_PORT_blue"], v["BG_PORT_green"]) == ("9205", "9206")
    # Chaque chemin est sous un préfixe fixe, jamais une valeur de la déclaration.
    for cle in ("BG_TREE", "BG_UPSTREAM", "BG_ACTIVE", "BG_LOCK", "BG_DRAIN", "CLE_SCW"):
        assert v[cle].startswith(("/opt/exemple/", "/etc/", "/var/lock/", "/usr/local/lib/exemple/"))


def test_un_role_non_declare_refuse(doc):
    del doc["roles"]["preprod"]
    with pytest.raises(decl.Refus, match="ne déclare pas ce rôle"):
        decl.variables(decl.valider(doc), "preprod")


def test_les_fichiers_d_environnement(doc, tmp_path):
    ecrits = decl.ecrire(doc, "prod", tmp_path)
    assert ecrits == ["/etc/exemple/prod/app.env", "/etc/exemple/prod/lanceur.env",
                      "/etc/exemple/prod/port-blue.env", "/etc/exemple/prod/port-green.env"]
    lanceur = (tmp_path / "etc/exemple/prod/lanceur.env").read_text()
    assert "OTO_SECRETS_OPTIONNELS=MISTRAL_API_KEY OTO_MCP_LOGTO_M2M_SECRET\n" in lanceur
    app = (tmp_path / "etc/exemple/prod/app.env").read_text()
    assert "OTO_LEGAL_DOCS='{\"cgu\"" in app


@pytest.mark.parametrize("valeur, ecrit", [
    ('{"a": 1}', "'{\"a\": 1}'"),
    ("fin  ", "'fin  '"),
    ("l'x \\ \"y\"", '"l\'x \\\\ \\"y\\""'),
])
def test_une_valeur_se_cite_pour_systemd(valeur, ecrit):
    # Relu par systemd à l'identique (vérifié avec `systemd-run -p EnvironmentFile=`).
    assert decl.valeur_env(valeur) == ecrit


def test_la_ligne_de_commande(doc, tmp_path, capsys):
    assert decl.main(["d", "verifier", str(DECLARATION)]) == 0
    assert decl.main(["d", "variables", str(DECLARATION), "prod"]) == 0
    assert "BG_LANCEUR=versionne" in capsys.readouterr().out
    assert decl.main(["d", "variables", str(DECLARATION), "staging"]) == 2
    mauvaise = tmp_path / "m.json"
    mauvaise.write_text("{")
    assert decl.main(["d", "verifier", str(mauvaise)]) == 1
    assert "déclaration illisible" in capsys.readouterr().err
