#!/usr/bin/env python3
"""Garde de mise en production : ce tag vise-t-il un arbre du tronc DÉJÀ jugé vert ?

DEPUIS LE 19/11/2026, LE TRONC NE DÉPLOIE PLUS. Jusque-là, chaque poussée sur `main`
partait en préproduction, et cette garde exigeait le run de préproduction vert du sha du
tag. La préproduction de l'instance principale est retirée : `main` n'est que le tronc,
`ci.yml` y juge chaque poussée sans rien servir, et seul un tag déploie, en production.
La garde exige donc désormais deux choses, et refuse sinon :

(a) LE SHA DU TAG EST SUR `main` — égal à sa pointe ou ancêtre de celle-ci. Un tag posé
    sur un commit d'une branche, ou retiré du tronc par une réécriture, est refusé.

(b) LE JOB `test` DE `ci.yml` A CONCLU `success` SUR CE SHA EXACT, dans un run poussé sur
    `main`. Ce job joue les volets touchés et le socle de gardes transverses
    (`scripts/selection_tests.py`) ; la suite COMPLÈTE se joue au tag, une seule fois, dans
    le job `test` de `deploy.yml`, que `deploy` attend (#1185). Cette garde ne prétend pas,
    à elle seule, que la suite entière est passée.

CE QU'ELLE EXIGE DU RUN, ET POURQUOI CHAQUE FILTRE EST LÀ.

1. `event=push` ET `head_branch=main`. Sur un événement `pull_request`, le `head_sha` d'un
   run est le commit de FUSION fabriqué par GitHub, pas celui de la branche : un run vert
   sur une PR ne dit rien de l'arbre qu'un tag désigne.

2. Le run doit venir de `.github/workflows/ci.yml` — le CHEMIN, pas le nom affiché. Un nom
   de workflow se renomme dans un commit ; le chemin est ce qui identifie le fichier. La
   garde voyage avec le tag (`deploy.yml` l'extrait de l'arbre du tag) : un tag posé sur un
   sha d'avant le 19/11 exécute l'ancienne garde, qui cherche l'ancien workflow.

3. Le run doit conclure `success` ET porter un job nommé `test` qui conclut `success`.
   **Les deux.** La conclusion d'un run est `success` alors même que certains de ses jobs
   sont `skipped`. Une garde qui se contenterait de la conclusion du run accepterait donc
   un arbre dont la sélection n'a jamais tourné.

4. Un run encore en cours n'est pas un verdict. Il est nommé comme tel, pas confondu avec
   une absence.

CE QU'IL DIT QUAND IL REFUSE.

Un refus qui ne nomme pas ce qu'il a lu fait rejouer le même geste. Chaque refus énumère
donc les runs trouvés, leur identifiant, leur date, et la conclusion exacte qui a été lue.

Sortie : 0 = le sha est sur le tronc et un verdict vert le couvre ; 1 = refus ;
2 = injugeable (l'API n'a pas répondu). 2 refuse aussi : une garde qui ne sait pas ne
laisse pas passer.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Le workflow du tronc, par son CHEMIN (cf. §2 du préambule).
WORKFLOW_TRONC = ".github/workflows/ci.yml"
# Le job dont la conclusion fait foi. Il joue la SÉLECTION du push (volets touchés +
# socle), pas la suite complète, qui tourne au tag (#1185).
JOB_EXIGE = "test"
# Ce que rend l'API `compare/<sha>...main` quand le sha est sur le tronc : la pointe de
# `main` est en avance sur lui (il en est un ancêtre), ou c'est le même commit.
SUR_LE_TRONC = ("ahead", "identical")

CODE_ACCEPTE = 0
CODE_REFUS = 1
CODE_INJUGEABLE = 2

API = "https://api.github.com"


class Injugeable(Exception):
    """L'API n'a pas répondu : on ne sait pas, donc on refuse (jamais un vert par défaut)."""


class Verdict:
    """Ce que la garde a conclu, et le texte qui le justifie."""

    def __init__(self, accepte: bool, message: str) -> None:
        self.accepte = accepte
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        etat = "ACCEPTE" if self.accepte else "REFUS"
        return "Verdict({}, {!r})".format(etat, self.message)


def _conclusion_lisible(run: dict) -> str:
    """Ce qu'on a lu du run, en une phrase — jamais un mot nu sorti de son contexte."""
    if run.get("status") != "completed":
        return "le run est encore « {} »".format(run.get("status"))
    return "le run conclut « {} »".format(run.get("conclusion"))


def _run_en_vol(rid, cree, jobs: list) -> str:
    """Un run qui n'a pas conclu — mais TOUS les « pas conclu » ne se valent pas.

    Rencontré en vrai le 08/09/2026 sur `324ef83c` : le job `test` était vert depuis
    1 min 30, et le run tournait encore parce que `contrat-front` n'avait pas rendu son
    verdict (15 s de plus). Lu vite, « le run est encore in_progress » se comprend « les
    tests ne sont pas finis », donc « j'en ai pour huit minutes », donc « je contourne ».
    La vraie réponse était « attends une minute ».

    Cette forme de refus est dangereuse parce qu'elle RESSEMBLE aux deux autres. Le
    message doit donc dire laquelle des deux on est en train de lire.
    """
    restants = [
        str(j.get("name")) for j in jobs if j.get("status") != "completed"
    ]
    vise = [j for j in jobs if j.get("name") == JOB_EXIGE]
    tete = "  - run {} du {} : le run n'a pas conclu".format(rid, cree)

    if vise and vise[0].get("conclusion") == "success":
        fin = vise[0].get("completed_at")
        return (
            "{}, MAIS LA SUITE A DÉJÀ PASSÉ — job « {} » : success{}. Ce qui reste à "
            "conclure : {}. ⚠️ Ce n'est PAS « les tests ne sont pas finis » : l'attente "
            "restante se compte en dizaines de secondes, pas en minutes. Attendre, "
            "surtout pas contourner.".format(
                tete,
                JOB_EXIGE,
                " (terminé à {})".format(fin) if fin else "",
                ", ".join(restants) or "(rien d'identifiable)",
            )
        )
    if vise and vise[0].get("conclusion") not in (None, "success"):
        # Volontairement pas « le run finira rouge » : un `skipped` ne rend pas un run
        # rouge. Ce qui est vrai dans les deux cas, c'est qu'aucun verdict vert sur la
        # suite ne sortira de CE run — donc que patienter ne sert à rien.
        return (
            "{} et son job « {} » conclut DÉJÀ « {} » — aucun verdict vert du tronc "
            "ne sortira de ce run, inutile d'attendre.".format(
                tete, JOB_EXIGE, vise[0].get("conclusion")
            )
        )
    return (
        "{} et la sélection du tronc tourne ENCORE (job « {} » : {}). Compter la "
        "durée de la sélection, au plus celle de la suite complète (~9 min).".format(
            tete,
            JOB_EXIGE,
            (vise[0].get("status") if vise else "pas encore démarré"),
        )
    )


def juger(tag: str, sha: str, position: str, runs: list, jobs_de) -> Verdict:
    """Décide, à partir de la position du sha, des runs déjà filtrés par l'API et d'un
    accès aux jobs.

    `position` : le `status` de l'API `compare/<sha>...main` (`ahead`, `identical`,
    `behind`, `diverged`).
    `runs` : les runs `event=push` + `head_branch=main` portant CE `head_sha`.
    `jobs_de(run_id)` : rend la liste des jobs d'un run.

    Fonction pure de ses entrées : c'est ce qui la rend éprouvable sans réseau.
    """
    if position not in SUR_LE_TRONC:
        return Verdict(
            False,
            "REFUS : l'arbre de {} ({}) n'est pas sur `main` — la comparaison avec la "
            "pointe du tronc rend « {} ». Seul un commit du tronc part en production : "
            "le fusionner dans `main`, attendre son run CI vert, puis retaguer.".format(
                tag, sha, position
            ),
        )

    candidats = [r for r in runs if r.get("path") == WORKFLOW_TRONC]
    candidats.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    if not candidats:
        lignes = [
            "REFUS : aucun run CI du tronc ne couvre l'arbre de {} ({}).".format(tag, sha),
            "Ce qui a été lu : aucun run de « {} » sur ce sha exact "
            "(événement push, branche main).".format(WORKFLOW_TRONC),
        ]
        autres = [r for r in runs if r.get("path") != WORKFLOW_TRONC]
        if autres:
            lignes.append(
                "Sur ce sha, {} run(s) d'AUTRES workflows existent — ils ne valent pas "
                "verdict : {}".format(
                    len(autres), ", ".join(sorted({str(r.get("path")) for r in autres}))
                )
            )
        lignes.append(
            "Ce sha est sur `main`, mais aucun run ne l'a jugé : il est arrivé au milieu "
            "d'une poussée de plusieurs commits, ou son run a été purgé. La production "
            "ne déploie qu'un sha dont le tronc a rendu le verdict : taguer la pointe "
            "d'une poussée dont le run CI est vert."
        )
        return Verdict(False, "\n".join(lignes))

    constats = []
    for run in candidats:
        rid = run.get("id")
        cree = run.get("created_at")
        if run.get("status") != "completed":
            # On consulte les jobs même pour un run en vol : « pas conclu » recouvre
            # deux situations très différentes, et les confondre pousse à contourner.
            constats.append(_run_en_vol(rid, cree, jobs_de(rid)))
            continue
        if run.get("conclusion") != "success":
            constats.append(
                "  - run {} du {} : {}".format(rid, cree, _conclusion_lisible(run))
            )
            continue

        jobs = jobs_de(rid)
        vise = [j for j in jobs if j.get("name") == JOB_EXIGE]
        if not vise:
            constats.append(
                "  - run {} du {} : le run conclut « success », mais il ne porte AUCUN job "
                "nommé « {} » — jobs présents : {}".format(
                    rid,
                    cree,
                    JOB_EXIGE,
                    ", ".join(str(j.get("name")) for j in jobs) or "(aucun)",
                )
            )
            continue

        job = vise[0]
        if job.get("conclusion") != "success":
            constats.append(
                "  - run {} du {} : le run conclut « success », mais son job « {} » conclut "
                "« {} » — un job sauté ou rouge ne rend pas un run rouge, c'est "
                "exactement le cas que cette garde ferme".format(
                    rid, cree, JOB_EXIGE, job.get("conclusion")
                )
            )
            continue

        return Verdict(
            True,
            "Verdict réutilisé : l'arbre {} est sur `main`, et le run {} du {} l'a jugé "
            "(job « {} » : success). La suite complète se joue au tag, avant le "
            "déploiement.".format(sha, rid, cree, JOB_EXIGE),
        )

    lignes = [
        "REFUS : aucun run CI VERT du tronc ne couvre l'arbre de {} ({}).".format(
            tag, sha
        ),
        "Ce qui a été lu, sur {} run(s) CI du tronc trouvés sur ce sha exact :".format(
            len(candidats)
        ),
    ]
    lignes.extend(constats)
    return Verdict(False, "\n".join(lignes))


# ─────────────────────────────────────────────────────────────────────────────
# L'accès réseau. Volontairement en urllib de la bibliothèque standard : le job
# `deploy` tourne sur le runner auto-hébergé de la box, dont on ne contrôle pas
# l'outillage (rien ne garantit `gh` ni `pip`). Le jeton est lu dans
# l'environnement et posé en en-tête DANS le processus — jamais en argument de
# commande, où il vivrait dans /proc à la vue de tout l'hôte.


def _api(chemin: str, jeton: str) -> dict:
    requete = urllib.request.Request(API + chemin)
    requete.add_header("Authorization", "Bearer " + jeton)
    requete.add_header("Accept", "application/vnd.github+json")
    requete.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            return json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as erreur:
        raise Injugeable(
            "GET {} → HTTP {}. La garde n'a rien pu juger.".format(chemin, erreur.code)
        )
    except Exception as erreur:  # réseau, DNS, timeout, JSON illisible
        raise Injugeable("GET {} → {}. La garde n'a rien pu juger.".format(chemin, erreur))


def main(argv: list) -> int:
    tag = (argv[1] if len(argv) > 1 else os.environ.get("DEPLOY_REF", "")).strip()
    depot = os.environ.get("GITHUB_REPOSITORY", "").strip()
    jeton = os.environ.get("GITHUB_TOKEN", "").strip()

    if not tag or not depot or not jeton:
        print(
            "REFUS : la garde n'a pas ses entrées (tag={!r}, dépôt={!r}, jeton={}).".format(
                tag, depot, "présent" if jeton else "ABSENT"
            ),
            file=sys.stderr,
        )
        return CODE_INJUGEABLE

    try:
        # `/commits/<tag>` déréférence un tag ANNOTÉ jusqu'au commit (vérifié le 08/09
        # sur v1.241.0 : la ref brute rend un objet `tag`, cet appel rend le commit).
        # On ne se fie PAS à `github.sha` : sur un `workflow_dispatch` ou un
        # `workflow_call`, il vaut le sommet du ref dispatché, pas la cible du tag.
        sha = _api("/repos/{}/commits/{}".format(depot, tag), jeton).get("sha")
        if not sha:
            raise Injugeable("le tag {} ne se résout sur aucun commit".format(tag))

        # `<sha>...main` : `ahead` = main est en avance sur le sha, qui en est donc un
        # ancêtre ; `identical` = c'est la pointe. `behind`/`diverged` : hors du tronc.
        position = _api(
            "/repos/{}/compare/{}...main".format(depot, sha), jeton
        ).get("status", "")

        runs = _api(
            "/repos/{}/actions/runs?head_sha={}&event=push&branch=main&per_page=100".format(
                depot, sha
            ),
            jeton,
        ).get("workflow_runs", [])

        cache = {}

        def jobs_de(run_id):
            if run_id not in cache:
                cache[run_id] = _api(
                    "/repos/{}/actions/runs/{}/jobs?per_page=100".format(depot, run_id),
                    jeton,
                ).get("jobs", [])
            return cache[run_id]

        verdict = juger(tag, sha, position, runs, jobs_de)
    except Injugeable as erreur:
        print("REFUS (injugeable) : {}".format(erreur), file=sys.stderr)
        return CODE_INJUGEABLE

    print(verdict.message)
    return CODE_ACCEPTE if verdict.accepte else CODE_REFUS


if __name__ == "__main__":
    sys.exit(main(sys.argv))
