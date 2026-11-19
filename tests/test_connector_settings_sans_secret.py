"""`connector_settings` ne porte aucun secret — la garde, et sa sonde sur le code.

La table est en clair (`value TEXT`). Les secrets d'application OAuth de l'instance sont
rangés au coffre (`credentials_store.set_app_secret`, lus par `auth/app_instance`) ; la
table refuse toute clé dont le NOM a la forme d'un secret, sur une liste fermée de motifs,
à son écrivain unique (`db/connector_settings.set_connector_setting`). Une seule exception
nommée : `planity` / `firebase_api_key`, clé publique servie au navigateur.

Deux crans :
1. l'écrivain refuse, d'un refus nommé, AVANT toute connexion ;
2. une sonde AST sur `oto_mcp/` : toute clé de réglage que le code nomme — argument d'un
   accesseur de la table, ou tuple `_REGLAGES` / `_SETTINGS` d'un module qui ne passe pas
   par `auth/app_instance` — est jugée par le même prédicat. Une nouvelle clé de forme
   secret posée comme réglage fait rougir ce test, pas la prod.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from oto_mcp.db import connector_settings as store

RACINE = Path(__file__).resolve().parents[1] / "oto_mcp"

# Les accesseurs de la table, et la position de leur argument `key`.
_POSITION_DE_LA_CLE = {
    "set_connector_setting": 3,
    "get_connector_setting": 3,
    "clear_connector_setting": 3,
    "list_connector_settings": 0,
    "org_settings_for_key": 1,
    "scoped_settings_for_key": 0,
}


# ── 1. l'écrivain ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cle", ["client_secret", "app_secret", "user_client_secret",
                                 "signing_secret", "password", "bot_token", "api_key",
                                 "private_key", "Client_Secret"])
def test_l_ecrivain_refuse_une_cle_de_forme_secret(monkeypatch, cle):
    def _jamais():
        raise AssertionError("le refus doit précéder toute connexion")
    monkeypatch.setattr(store, "_connect", _jamais)
    with pytest.raises(store.ReglageSecretRefuse, match=cle):
        store.set_connector_setting("platform", "platform", "microsoft", cle, "x")


def test_la_copie_entre_cartes_refuse_un_secret(monkeypatch):
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(store.ReglageSecretRefuse):
        store.copy_platform_settings("sharepoint", "microsoft", ("client_id", "client_secret"))


@pytest.mark.parametrize("cle", ["client_id", "app_id", "config_id", "cardinality",
                                 "access_level", "personal_key", "platform.ops",
                                 "charge_cap_usd", "runner.org_key_required",
                                 "client_id@https://example.test/api/x/oauth/callback"])
def test_les_reglages_publics_passent(cle):
    assert not store.a_la_forme_d_un_secret("microsoft", cle)


def test_une_seule_exception_nommee():
    """La liste des exceptions est FERMÉE : en ajouter une est un acte, relu ici."""
    assert set(store.EXCEPTIONS_PUBLIQUES) == {("planity", "firebase_api_key")}
    assert not store.a_la_forme_d_un_secret("planity", "firebase_api_key")
    # L'exception vaut pour CE connecteur seulement.
    assert store.a_la_forme_d_un_secret("autre", "firebase_api_key")


# ── 2. la sonde sur le code ───────────────────────────────────────────────────

def _constantes(arbre: ast.Module) -> dict:
    """Les constantes de module : `NOM = "texte"` et `NOM = ("a", "b")`."""
    out: dict = {}
    for noeud in arbre.body:
        if isinstance(noeud, ast.Assign) and len(noeud.targets) == 1 \
                and isinstance(noeud.targets[0], ast.Name):
            try:
                out[noeud.targets[0].id] = ast.literal_eval(noeud.value)
            except (ValueError, SyntaxError, TypeError):
                continue
    return out


def _connecteur(consts: dict, chemin: Path) -> str:
    """Le connecteur du module : sa constante, sinon le nom du fichier
    (`tools/planity_session.py` → `planity`)."""
    for nom in ("CONNECTOR", "_NAME", "NAME"):
        if isinstance(consts.get(nom), str):
            return consts[nom]
    return chemin.stem.removesuffix("_session")


def _cles_nommees(arbre: ast.Module, consts: dict):
    """(ligne, clé) de chaque clé que le module passe à un accesseur de la table."""
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Call):
            continue
        f = noeud.func
        nom = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if nom not in _POSITION_DE_LA_CLE:
            continue
        arg = next((k.value for k in noeud.keywords if k.arg == "key"), None)
        pos = _POSITION_DE_LA_CLE[nom]
        if arg is None and len(noeud.args) > pos \
                and not any(isinstance(a, ast.Starred) for a in noeud.args[:pos + 1]):
            arg = noeud.args[pos]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield noeud.lineno, arg.value
        elif isinstance(arg, ast.Name) and isinstance(consts.get(arg.id), str):
            yield noeud.lineno, consts[arg.id]


def _infractions() -> list[str]:
    out = []
    for chemin in sorted(RACINE.rglob("*.py")):
        if chemin.name == "connector_settings.py" and chemin.parent.name == "db":
            continue
        source = chemin.read_text(encoding="utf-8")
        if "connector_setting" not in source:
            continue
        arbre = ast.parse(source)
        consts = _constantes(arbre)
        connecteur = _connecteur(consts, chemin)
        relatif = chemin.relative_to(RACINE.parent)
        for ligne, cle in _cles_nommees(arbre, consts):
            if store.a_la_forme_d_un_secret(connecteur, cle):
                out.append(f"{relatif}:{ligne} — clé {cle!r} passée à connector_settings")
        if "app_instance" in source:
            continue          # ses réglages sont partagés public / coffre par le prédicat
        for nom, valeur in consts.items():
            if ("REGLAGE" in nom or "SETTINGS" in nom) and isinstance(valeur, tuple):
                for cle in valeur:
                    if isinstance(cle, str) and store.a_la_forme_d_un_secret(
                            connecteur, cle):
                        out.append(f"{relatif} — {nom} porte {cle!r}, lu dans "
                                   "connector_settings sans passer par auth/app_instance")
    return out


def test_aucune_cle_de_forme_secret_n_est_un_reglage():
    infractions = _infractions()
    assert not infractions, (
        "Un secret ne se range pas dans connector_settings (table en clair) : lire et "
        "poser par auth/app_instance et credentials_store.set_app_secret —\n  "
        + "\n  ".join(infractions))


def test_la_sonde_voit_une_infraction(tmp_path, monkeypatch):
    """Contre-épreuve : un module qui pose `bot_token` comme réglage est vu."""
    paquet = tmp_path / "oto_mcp"
    paquet.mkdir()
    (paquet / "faux.py").write_text(
        'CONNECTOR = "faux"\n_REGLAGES = ("client_id", "bot_token")\n'
        "def f(store):\n"
        '    store.set_connector_setting("platform", "platform", CONNECTOR, "api_key", "v")\n'
        "    return store.list_connector_settings()\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "RACINE", paquet)
    trouvees = "\n".join(_infractions())
    assert "'api_key'" in trouvees and "'bot_token'" in trouvees, trouvees
