#!/usr/bin/env python3
"""Lanceur générique du backend : tire les secrets de l'instance, puis exécute le serveur.

oto-backend#967. Ce fichier est VERSIONNÉ et part avec le tag : l'unité systemd d'une
instance l'exécute depuis l'arbre de sa couleur (`<arbre>/.venv/bin/python
<arbre>/deploy/lanceur_secrets.py [args oto-mcp…]`), il n'est ni propagé ni édité sur
la machine. Il ne connaît AUCUN identifiant : ce qu'il tire se dérive de l'inventaire
(`oto_mcp/env_secrets.py`), où il le tire se déclare dans l'environnement de l'unité.

CE QU'IL TIRE
- toujours, les secrets REQUIS de l'inventaire (`env_secrets.secrets_requis()`) ;
- en plus, les secrets facultatifs que l'instance DÉCLARE porter
  (`OTO_SECRETS_OPTIONNELS`, noms séparés par des espaces — la variable est exigée,
  vide si l'instance n'en porte aucun).
Un secret introuvable refuse le démarrage, qu'il soit requis ou déclaré : une absence
muette ferait tourner une instance sans ce qu'on croit lui avoir donné.

OÙ IL LE TIRE — le Secret Manager Scaleway du projet DE L'INSTANCE, par nom :
  OTO_SECRETS_REGION    région (ex. fr-par)
  OTO_SECRETS_PROJET    identifiant du projet Scaleway de l'instance
  OTO_SECRETS_CHEMIN    chemin des secrets du rôle (ex. /prod, /preprod) : chaque rôle
                        a les siens, dont sa propre `DATABASE_URL`
  $CREDENTIALS_DIRECTORY/scw   la clé secrète d'API qui lit ces secrets, passée par
                        `LoadCredential=scw:…` — jamais dans l'environnement, jamais
                        dans le `.env`, et lisible du seul service.
Le secret porte le NOM de la variable (`OTO_MCP_MASTER_KEY`…), la dernière version
activée fait foi.

CE QU'IL REFUSE
- une variable secrète déjà présente dans l'environnement : le `.env` ne porte que le
  non-secret, et un secret qui y traîne serait une seconde source qui divergerait ;
- un facultatif déclaré qui n'est pas un secret de l'inventaire, ou qui ne doit jamais
  entrer dans le serveur (`env_secrets.HORS_SERVEUR`).

Il n'écrit jamais une valeur : le journal nomme les secrets tirés, pas leur contenu.

CE QU'IL DÉMARRE — une seule chose par appel, choisie par son PREMIER argument :
  (rien d'autre)       le serveur, `<arbre>/.venv/bin/oto-mcp [args…]` (`maintenance all`,
                       `migrer upgrade head`…) ;
  --script CHEMIN [args…]  un script de l'arbre, sous son Python : `<arbre>/.venv/bin/python
                       <arbre>/CHEMIN [args…]` (ex. `deploy/archive_tool_calls.py`, l'archive
                       du journal). Il reçoit le MÊME environnement, secrets compris : une
                       seule source de secrets, pas un `.env` de plus. Le chemin est relatif à
                       l'arbre, un fichier .py qui s'y trouve — rien n'en sort ;
  --noms               ne démarre rien : lit les secrets comme pour un démarrage puis
                       imprime, un par ligne et triés, les NOMS de l'environnement final —
                       jamais une valeur. C'est le contrôle d'un passage au lanceur : à
                       comparer, par noms, aux `/proc/<pid>/environ` du service qu'il remplace.
Ces trois formes ne se combinent pas : `--script` et `--noms` doivent venir en premier.

Notre production n'utilise PAS encore ce lanceur : son `start-encrypted.sh` vit hors git
et se propage d'une couleur à l'autre (`BG_LANCEUR=propage`, cf.
`deploy/start-encrypted.sh`). La passer ici est un geste à part, décrit pas à pas dans
docs/instance-cible.md (§Passer notre box au lanceur).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARBRE = Path(__file__).resolve().parent.parent
if str(ARBRE) not in sys.path:
    sys.path.insert(0, str(ARBRE))

from oto_mcp import env_secrets  # noqa: E402 — l'arbre d'abord, pour lire SON inventaire

API = ("https://api.scaleway.com/secret-manager/v1beta1/regions/{region}"
       "/secrets-by-path/versions/latest_enabled/access")
CONFIGURATION = ("OTO_SECRETS_REGION", "OTO_SECRETS_PROJET", "OTO_SECRETS_CHEMIN")
OPTIONNELS = "OTO_SECRETS_OPTIONNELS"
DELAI_S = 15


class Refus(Exception):
    """Le lanceur refuse de démarrer le serveur ; le message nomme pourquoi."""


def a_tirer(env: dict) -> list[str]:
    """Les secrets à tirer pour cet environnement, requis d'abord."""
    if OPTIONNELS not in env:
        raise Refus(f"{OPTIONNELS} n'est pas déclarée (vide si l'instance n'en porte aucun)")
    requis = list(env_secrets.secrets_requis())
    optionnels = env[OPTIONNELS].split()
    for nom in optionnels:
        if not env_secrets.est_secret(nom):
            raise Refus(f"{nom} (dans {OPTIONNELS}) n'est pas un secret de l'inventaire")
        if nom in env_secrets.HORS_SERVEUR:
            raise Refus(f"{nom} n'entre jamais dans l'environnement du serveur")
        if nom in requis:
            raise Refus(f"{nom} est requis : il est toujours tiré, ne pas le déclarer")
    if len(set(optionnels)) != len(optionnels):
        raise Refus(f"{OPTIONNELS} nomme deux fois le même secret")
    return requis + optionnels


def verifier_sans_secret(env: dict, noms: list[str]) -> None:
    """Aucun secret ne doit arriver par l'environnement (le `.env`)."""
    deja = sorted(n for n in env if env_secrets.est_secret(n) or n in noms)
    if deja:
        raise Refus("secret(s) déjà dans l'environnement — le .env ne porte que le "
                    "non-secret : " + ", ".join(deja))


def configuration(env: dict) -> tuple[str, str, str]:
    manque = [n for n in CONFIGURATION if not env.get(n, "").strip()]
    if manque:
        raise Refus("configuration du lanceur absente : " + ", ".join(manque))
    region, projet, chemin = (env[n].strip() for n in CONFIGURATION)
    if not chemin.startswith("/"):
        raise Refus(f"OTO_SECRETS_CHEMIN doit être absolu (reçu {chemin!r})")
    return region, projet, chemin


def jeton(env: dict) -> str:
    dossier = env.get("CREDENTIALS_DIRECTORY", "")
    if not dossier:
        raise Refus("aucune clé d'API : l'unité doit passer LoadCredential=scw:<fichier>")
    try:
        valeur = (Path(dossier) / "scw").read_text(encoding="utf-8").strip()
    except OSError as erreur:
        raise Refus(f"clé d'API illisible ({erreur})") from erreur
    if not valeur:
        raise Refus("clé d'API vide")
    return valeur


def tirer(nom: str, region: str, projet: str, chemin: str, cle: str,
          ouvrir=urllib.request.urlopen) -> str:
    requete = urllib.request.Request(
        API.format(region=urllib.parse.quote(region, safe="")) + "?"
        + urllib.parse.urlencode({"project_id": projet, "secret_name": nom,
                                  "secret_path": chemin}),
        headers={"X-Auth-Token": cle})
    try:
        with ouvrir(requete, timeout=DELAI_S) as reponse:
            donnee = json.loads(reponse.read().decode("utf-8"))["data"]
        valeur = base64.b64decode(donnee).decode("utf-8").rstrip("\r\n")
    except urllib.error.HTTPError as erreur:
        raise Refus(f"{nom} introuvable dans {chemin} (HTTP {erreur.code})") from erreur
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as erreur:
        raise Refus(f"{nom} : lecture impossible ({type(erreur).__name__}: {erreur})") from erreur
    if not valeur:
        raise Refus(f"{nom} est vide dans {chemin}")
    return valeur


def preparer(env: dict, ouvrir=urllib.request.urlopen) -> tuple[dict, list[str]]:
    """L'environnement du serveur, secrets compris, et la liste de ce qui a été tiré."""
    noms = a_tirer(env)
    verifier_sans_secret(env, noms)
    region, projet, chemin = configuration(env)
    cle = jeton(env)
    complet = dict(env)
    for nom in noms:
        complet[nom] = tirer(nom, region, projet, chemin, cle, ouvrir)
    return complet, noms


def cible(arguments: list[str]) -> list[str] | None:
    """La commande que le lanceur exécute : le serveur, ou un script de l'arbre.

    Rend None pour `--noms` (rien à exécuter). Lève `Refus` sur une forme invalide.
    """
    if arguments[:1] == ["--noms"]:
        if arguments[1:]:
            raise Refus("--noms ne prend aucun autre argument")
        return None
    if arguments[:1] == ["--script"]:
        if len(arguments) < 2:
            raise Refus("--script attend le chemin du script, relatif à l'arbre")
        relatif = Path(arguments[1])
        chemin = (ARBRE / relatif).resolve()
        if relatif.is_absolute() or ARBRE not in chemin.parents or chemin.suffix != ".py":
            raise Refus(f"--script : {arguments[1]!r} n'est pas un fichier .py de l'arbre")
        if not chemin.is_file():
            raise Refus(f"--script : {arguments[1]} n'existe pas dans l'arbre")
        return [str(ARBRE / ".venv" / "bin" / "python"), str(chemin), *arguments[2:]]
    return [str(ARBRE / ".venv" / "bin" / "oto-mcp"), *arguments]


def main(argv: list[str]) -> int:
    try:
        commande = cible(argv[1:])
        env, noms = preparer(dict(os.environ))
    except Refus as refus:
        print(f"lanceur : REFUS — {refus}", file=sys.stderr)
        return 1
    if commande is None:
        print("\n".join(sorted(env)))
        return 0
    print(f"lanceur : {len(noms)} secret(s) tiré(s) de {env['OTO_SECRETS_CHEMIN']} : "
          + " ".join(noms), file=sys.stderr)
    os.execve(commande[0], commande, env)
    return 1  # execve ne rend pas la main


if __name__ == "__main__":
    sys.exit(main(sys.argv))
