"""Les journaux du workflow d'une cible ne la désignent jamais (#967, décision D5 d'Alexis).

Ce dépôt est public, les journaux de ses runs aussi. GitHub imprime l'`env:` de chaque
step en tête de son journal et ne masque d'office que les secrets : tout ce qui désigne
la cible est donc un SECRET de son environnement, jamais une variable ni une entrée ; ce
qu'on en dérive est masqué (`::add-mask::`) par le premier geste de chaque job, et les
scripts parlent de « la préprod de la cible », jamais de son hôte. Ce test fige :
- aucune valeur de la cible n'est lue depuis `vars.` ni depuis une entrée (seule
  `cible`, le nom de l'environnement, public par nature) — que le workflow parte à la
  main ou soit appelé par le dépôt du propriétaire de la cible ;
- appelé, le dépôt appelant (qui désigne le propriétaire) est masqué aussi ;
- le masquage est le PREMIER geste de chaque job, et d'abord du premier ;
- tout secret qui désigne la cible et l'entrée `cible` que voit un job y sont masqués ;
- aucun step n'affiche une valeur de la cible (echo, printf, résumé du job) ;
- le masquage, exécuté sur une déclaration d'exemple, cache tout ce qui la désigne —
  hôtes, URL de `/api/version`, domaines, instance, chemins dérivés, slug, marque,
  adresses, hôte et utilisateur SSH, dépôt du consommateur — et laisse lisibles les rôles ;
- `constater.sh`, `protection.sh` et `declaration.py verifier` ne citent ni hôte, ni
  environnement, ni instance.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

_RACINE = pathlib.Path(__file__).resolve().parents[1]
_TEXTE = (_RACINE / ".github" / "workflows" / "deploy-cible.yml").read_text(encoding="utf-8")
_WF = yaml.safe_load(_TEXTE)
_JOBS = _WF["jobs"]
_EXEMPLE = _RACINE / "tests" / "deploy" / "declaration_exemple.json"

# Ce qui désigne la cible, et doit venir d'un SECRET de son environnement.
_DESIGNE = ("CIBLE_DECLARATION", "CIBLE_CONSOMMATEUR", "CIBLE_SSH_HOTE",
            "CIBLE_SSH_UTILISATEUR", "CIBLE_SSH_KNOWN_HOSTS", "CIBLE_DECLENCHEURS")
# Ce qui porte une valeur de la cible dans le workflow : ces secrets, toute variable
# (interdite, mais repérée si elle revenait), et l'entrée qui nomme l'environnement.
_VALEUR_CIBLE = re.compile(
    rf"\$\{{\{{\s*(secrets\.(?:{'|'.join(_DESIGNE)})\b|vars\.[A-Z_]+|inputs\.cible)\s*\}}\}}")
_ENTREES = {"cible", "tag", "etape", "action", "acces"}
_MASQUE = "::add-mask::"


def _porteurs(env: dict | None) -> dict[str, str]:
    """Les variables d'environnement qui portent une valeur de la cible → leur source."""
    return {nom: m.group(1) for nom, val in (env or {}).items()
            if (m := _VALEUR_CIBLE.search(str(val)))}


def _cite(script: str, nom: str) -> bool:
    return re.search(rf"\$\{{?{nom}\b", script) is not None


def _etape_masque(job: dict) -> dict:
    return job["steps"][0]


_AFFICHE = re.compile(r"^\s*(?:echo|printf)\b(?!.*>\s*\"?\$RUNNER_TEMP).*\$\{?(?:CIBLE\b|CIBLE_)",
                      re.MULTILINE)


def _ecarts(wf: dict) -> list[str]:
    """Tout ce qui, dans un workflow, afficherait une valeur de la cible sans masque."""
    ecarts = []
    for nom, job in wf["jobs"].items():
        etape = _etape_masque(job)
        if "uses" in etape or _MASQUE not in etape.get("run", ""):
            ecarts.append(f"{nom} : le premier geste n'est pas le masquage ({etape.get('name')})")
            continue
        # ce que voit le step de masquage : env du workflow, du job, et le sien
        visibles = {**_porteurs(wf.get("env")), **_porteurs(job.get("env")),
                    **_porteurs(etape.get("env"))}
        ecarts += [f"{nom} : {v} n'est pas masquée" for v in visibles
                   if not _cite(etape["run"], v)]
        if _VALEUR_CIBLE.search(str(job.get("name", ""))):
            ecarts.append(f"{nom} : le nom du job porte une valeur de la cible")
        for suivante in job["steps"][1:]:
            ou = f"{nom} / {suivante.get('name')}"
            # rien de la cible n'arrive par un step suivant sans être passé par le masque
            ecarts += [f"{ou} : {src} arrive sans masque"
                       for src in _porteurs(suivante.get("env")).values()
                       if src not in visibles.values()]
            ecarts += [f"{ou} : {cle} porte une valeur de la cible"
                       for cle in ("with", "run", "name", "if")
                       if _VALEUR_CIBLE.search(str(suivante.get(cle, "")))]
            run = suivante.get("run", "")
            if m := _AFFICHE.search(run):
                ecarts.append(f"{ou} : affiche « {m.group(0).strip()} »")
            ecarts += [f"{ou} : résumé « {l.strip()} »" for l in run.splitlines()
                       if "GITHUB_STEP_SUMMARY" in l
                       and re.search(r"\$\{?(CIBLE|NOM|DEPOT)\b", l)]
    return ecarts


# --- l'ordre : masquer d'abord, rien d'affiché sans masque -------------------------
def test_le_masquage_est_le_premier_geste_du_premier_job():
    premier = next(iter(_JOBS.values()))
    etape = _etape_masque(premier)
    assert "uses" not in etape and _MASQUE in etape["run"], etape.get("name")
    assert _cite(etape["run"], "CIBLE")


def test_aucune_valeur_de_la_cible_ne_s_affiche_sans_masque():
    assert _ecarts(_WF) == []


def _lectures_hors_secret(texte: str, entrees) -> list[str]:
    """Une valeur de la cible lue depuis `vars.` ou depuis une entrée autre que les
    quatre du workflow (dont `cible`, le nom de l'environnement)."""
    return ([f"vars.{m}" for m in re.findall(r"\bvars\.([A-Za-z_]+)", texte)]
            + [f"entrée {e}" for e in sorted(set(entrees) - _ENTREES)]
            + [f"inputs.{m}" for m in re.findall(r"\binputs\.([A-Za-z_]+)", texte)
               if m not in _ENTREES])


def test_ce_qui_designe_la_cible_vient_d_un_secret():
    for declencheur in _WF.get("on", _WF.get(True)).values():
        assert _lectures_hors_secret(_TEXTE, declencheur["inputs"]) == []
    for nom in _DESIGNE:
        assert f"secrets.{nom} }}}}" in _TEXTE, nom


def test_tout_secret_qui_designe_la_cible_est_masque_d_abord():
    masque = _etape_masque(_JOBS["monter"])["run"]
    porteurs = _porteurs(_JOBS["monter"]["env"])
    for secret in _DESIGNE:
        noms = [n for n, src in porteurs.items() if src == f"secrets.{secret}"]
        assert noms and all(_cite(masque, n) for n in noms), secret


def _copie():
    import copy
    return copy.deepcopy(_WF)


def test_la_regle_mord():
    """Chaque manière de lire ou d'afficher la cible sans secret ni masque est vue."""
    # une valeur de la cible revenue en variable, ou arrivée par une entrée
    assert _lectures_hors_secret(
        _TEXTE.replace("secrets.CIBLE_SSH_HOTE", "vars.CIBLE_SSH_HOTE"), _ENTREES)
    assert _lectures_hors_secret("run: echo ${{ inputs.hote }}", _ENTREES)
    assert _lectures_hors_secret("", _ENTREES | {"domaine"})
    # le masquage déplacé après le checkout
    wf = _copie()
    etapes = wf["jobs"]["monter"]["steps"]
    etapes.insert(0, etapes.pop(1))
    assert _ecarts(wf)
    # une variable de la cible qui n'est pas passée par le masque
    wf = _copie()
    wf["jobs"]["monter"]["steps"][3]["env"] = {"H": "${{ secrets.CIBLE_SSH_HOTE }}",
                                                "D": "${{ vars.CIBLE_AUTRE }}"}
    assert any("arrive sans masque" in e for e in _ecarts(wf))
    # un step qui affiche la déclaration, ou la cible dans le résumé
    wf = _copie()
    wf["jobs"]["monter"]["steps"][3]["run"] = 'echo "déclaration : $CIBLE_DECLARATION"'
    assert any("affiche" in e for e in _ecarts(wf))
    wf = _copie()
    wf["jobs"]["entrees"]["steps"][1]["run"] = 'echo "$ACTION → $CIBLE" >> "$GITHUB_STEP_SUMMARY"'
    assert any("résumé" in e for e in _ecarts(wf))
    # une variable interpolée dans un nom de step
    wf = _copie()
    wf["jobs"]["monter"]["steps"][3]["name"] = "vers ${{ secrets.CIBLE_SSH_HOTE }}"
    assert any("name porte" in e for e in _ecarts(wf))
    # un masquage qui oublie une variable
    wf = _copie()
    wf["jobs"]["monter"]["steps"][0]["run"] = 'echo "::add-mask::$CIBLE"'
    assert any("n'est pas masquée" in e for e in _ecarts(wf))


# --- le masquage, exécuté ----------------------------------------------------------
def _declaration_distincte() -> dict:
    """L'exemple, instance et slug distincts du domaine : chaque dérivation (libellé du
    domaine, domaines parents) est éprouvée pour elle-même."""
    doc = json.loads(_EXEMPLE.read_text(encoding="utf-8"))
    doc["instance"] = "cliente"
    for role in doc["roles"].values():
        role["env"]["OTO_TENANT_PRIMAIRE_SLUG"] = "cliente"
    return doc


_CONSOMMATEUR = {"nom": "front-exemple", "depot": "exemple-org/front-app",
                 "chemin": "contrat/openapi.json", "cle": True}
_ENV_CIBLE = {
    "CIBLE": "cible-exemple",
    "CIBLE_DECLARATION": json.dumps(_declaration_distincte(), indent=2),
    "CONSOMMATEUR": json.dumps(_CONSOMMATEUR),
    "CIBLE_SSH_HOTE": "ssh.exemple.test",
    "CIBLE_SSH_UTILISATEUR": "porte-exemple",
    "CIBLE_SSH_KNOWN_HOSTS": "ssh.exemple.test,[bastion.exemple.test]:2222 ssh-ed25519 AAAAC3Nz",
    "CIBLE_DECLENCHEURS": "alice-exemple, bob-exemple\ncarol-exemple",
}
_DECLENCHEURS = ("alice-exemple", "bob-exemple", "carol-exemple")


def _masques(env: dict[str, str]) -> list[str]:
    fini = subprocess.run(["/bin/bash", "-c", _etape_masque(_JOBS["monter"])["run"]],
                          env={"PATH": os.environ["PATH"], **env},
                          capture_output=True, text=True, timeout=30)
    assert fini.returncode == 0, fini.stdout + fini.stderr
    lignes = fini.stdout.splitlines()
    # il n'écrit RIEN d'autre que des masques : ni valeur, ni diagnostic
    assert all(l.startswith(_MASQUE) for l in lignes), lignes
    assert fini.stderr == ""
    return [l[len(_MASQUE):] for l in lignes]


def _journal(texte: str, masques: list[str]) -> str:
    """Ce que le runner afficherait : chaque occurrence d'un masque devient ***."""
    couvert = [False] * len(texte)
    for m in masques:
        for occ in re.finditer(re.escape(m), texte):
            couvert[occ.start():occ.end()] = [True] * len(m)
    sortie, i = [], 0
    while i < len(texte):
        if couvert[i]:
            while i < len(texte) and couvert[i]:
                i += 1
            sortie.append("***")
        else:
            sortie.append(texte[i])
            i += 1
    return "".join(sortie)


@pytest.fixture(scope="module")
def masques():
    return _masques(_ENV_CIBLE)


def test_le_masquage_cache_tout_ce_qui_designe_la_cible(masques):
    doc = json.loads(_ENV_CIBLE["CIBLE_DECLARATION"])
    hotes = [r["hote_public"] for r in doc["roles"].values()]
    # ce qui doit disparaître EN ENTIER du journal
    entieres = [
        *_ENV_CIBLE.values(),                       # l'en-tête d'un step : son env
        doc["instance"], doc["secrets"]["projet"],
        *hotes, *(f"https://{h}/api/version" for h in hotes),
        "exemple.test", "exemple",                  # le domaine et son libellé, seuls
        "bastion.exemple.test", "cible-exemple",
        *(v for r in doc["roles"].values() for k, v in r["env"].items()
          if k != "OTO_ENV" and any(c.isalpha() for c in v)),
        *(_CONSOMMATEUR[k] for k in ("nom", "depot", "chemin")), "exemple-org",
        *_DECLENCHEURS,                             # chaque login de la liste, seul
    ]
    for valeur in entieres:
        for ligne in valeur.splitlines():
            if any(c.isalnum() for c in ligne):
                assert _journal(ligne.strip(), masques) == "***", ligne
    # ce qui s'en dérive ou la cite : plus rien de la cible n'y paraît
    sys.path.insert(0, str(_RACINE / "deploy" / "cible"))
    import declaration
    derivees = [v for role in doc["roles"] for v in declaration.variables(doc, role).values()]
    journal = "\n".join([
        *derivees,                                  # chemins, unités, utilisateur, URL
        "curl: (6) Could not resolve host: mcp-preprod.exemple.test",
        "GET https://mcp.exemple.test/api/version -> 404",
        "git clone git@github.com:exemple-org/front-app.git",
        "ssh: connect to host bastion.exemple.test port 2222",
        "déclencheurs : bob-exemple et carol-exemple",
    ])
    vu = _journal(journal, masques)
    for trace in ("exemple", "cliente", doc["secrets"]["projet"], "bastion"):
        assert trace not in vu.lower(), [l for l in vu.splitlines() if trace in l.lower()]


def test_le_masquage_laisse_le_journal_lisible(masques):
    vu = _journal("Monter la preprod — deploy/cible/appeler.sh deployer prod v1.2.3 : true",
                  masques)
    assert vu == "Monter la preprod — deploy/cible/appeler.sh deployer prod v1.2.3 : true"


def test_une_declaration_illisible_a_ses_lignes_masquees():
    env = {**_ENV_CIBLE, "CIBLE_DECLARATION": '{\n  "instance": "exemple",\n  "roles": [',
           "CONSOMMATEUR": "", "CIBLE_SSH_KNOWN_HOSTS": ""}
    vu = _journal(env["CIBLE_DECLARATION"], _masques(env))
    assert "exemple" not in vu


def _masquer_la_cible(appelant: str) -> list[str]:
    run = _etape_masque(_JOBS["entrees"])["run"]
    fini = subprocess.run(["/bin/bash", "-c", run],
                          env={"PATH": os.environ["PATH"], "CIBLE": "une\ndeux",
                               "TRONC": _WF["env"]["TRONC"], "APPELANT": appelant},
                          capture_output=True, text=True, timeout=30)
    assert fini.returncode == 0, fini.stderr
    return fini.stdout.splitlines()


def test_une_cible_sur_plusieurs_lignes_est_masquee_ligne_a_ligne():
    assert _masquer_la_cible(_WF["env"]["TRONC"]) == [f"{_MASQUE}une", f"{_MASQUE}deux"]


def test_appele_le_depot_appelant_est_masque():
    """Le dépôt appelant nomme le propriétaire de la cible : masqué, lui et ses deux
    moitiés, avant tout geste (le tronc, public, reste lisible)."""
    assert _etape_masque(_JOBS["entrees"])["env"]["APPELANT"] == "${{ github.repository }}"
    assert _masquer_la_cible("proprio-exemple/infra-privee")[2:] == [
        f"{_MASQUE}proprio-exemple/infra-privee", f"{_MASQUE}proprio-exemple",
        f"{_MASQUE}infra-privee"]


def test_en_acces_ssh_l_adresse_de_la_machine_est_masquee():
    """En accès ssh, l'hôte est la machine elle-même — souvent une adresse IP, et le
    known_hosts peut la porter sous une autre forme (`[adresse]:22`). Adresses de
    documentation (RFC 5737)."""
    env = {**_ENV_CIBLE, "CIBLE_SSH_HOTE": "192.0.2.10",
           "CIBLE_SSH_KNOWN_HOSTS": "192.0.2.10,[198.51.100.7]:22 ssh-ed25519 AAAAC3Nz"}
    vu = _journal("ssh: connect to host 192.0.2.10 port 22: Connection refused\n"
                  "Warning: Permanently added '198.51.100.7'", _masques(env))
    assert "192.0.2" not in vu and "198.51.100" not in vu, vu


# --- les scripts ne citent ni hôte, ni environnement, ni instance ------------------
def _doublures(tmp_path, **corps) -> dict[str, str]:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for nom, c in corps.items():
        (bin_ / nom).write_text(f"#!/bin/bash\n{c}\n")
        (bin_ / nom).chmod(0o755)
    return {"PATH": f"{bin_}:/usr/bin:/bin"}


@pytest.mark.parametrize("curl, code", [
    ('echo \'{"ref": "v1.2.3"}\'', 0),
    ('echo \'{"ref": "v1.0.0"}\'', 1),
    ("exit 7", 1),
])
def test_constater_ne_cite_pas_l_hote(tmp_path, curl, code):
    fini = subprocess.run(
        ["/bin/bash", str(_RACINE / "deploy/cible/constater.sh"), str(_EXEMPLE), "preprod", "v1.2.3"],
        env=_doublures(tmp_path, curl=curl, sleep="exit 0"),
        capture_output=True, text=True, timeout=30)
    assert fini.returncode == code, fini.stdout + fini.stderr
    assert "la preprod de la cible" in fini.stdout
    assert "exemple" not in fini.stdout + fini.stderr


@pytest.mark.parametrize("reponse, code", [
    ('{"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}', 0),
    ('{"protection_rules": []}', 0),
    ('{"protection_rules": [], "deployment_branch_policy": {"protected_branches": true}}', 0),
    ('{"message": "Not Found"}', 1),
])
def test_protection_ne_cite_pas_l_environnement(tmp_path, reponse, code):
    (tmp_path / "reponse").write_text(reponse)
    fini = subprocess.run(
        ["/bin/bash", str(_RACINE / "deploy/cible/protection.sh"), "environnement",
         "proprio/depot", "exemple"],
        env={**_doublures(tmp_path, gh=f'cat "{tmp_path}/reponse"; exit {code}'),
             "GITHUB_OUTPUT": str(tmp_path / "sortie")},
        capture_output=True, text=True, timeout=30)
    assert fini.returncode == (0 if "User" in reponse or "protected_branches" in reponse else 1)
    assert "environnement de la cible" in fini.stdout.lower()
    assert "exemple" not in fini.stdout + fini.stderr


def test_verifier_ne_cite_pas_l_instance():
    fini = subprocess.run([sys.executable, str(_RACINE / "deploy/cible/declaration.py"),
                           "verifier", str(_EXEMPLE)],
                          capture_output=True, text=True, timeout=60)
    assert fini.returncode == 0, fini.stderr
    assert "déclaration conforme" in fini.stdout and "exemple" not in fini.stdout
