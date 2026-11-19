"""La garde de mise en production doit REFUSER, pas seulement laisser passer.

Depuis le 19/11/2026, le tronc ne déploie plus : la garde exige que le sha du tag soit sur
`main` et que le job `test` de `ci.yml` y ait conclu `success` (`scripts/garde_tronc_vert.py`).

Une garde qu'on n'a jamais vue mordre ne prouve rien. Le dépôt en porte déjà l'exemple :
l'étape « Refuser de publier un tronc rouge » de `release.yml` AVERTIT et continue quand
elle ne trouve aucun run — elle n'a donc jamais refusé personne, et personne ne s'en est
aperçu. Ces cas-ci existent pour que celle de `deploy.yml` ne finisse pas décorative.

Le cas central est le §5 : un run dont la CONCLUSION est `success` alors que son job `test`
est `skipped`. Un job sauté ne rend pas un run rouge, et c'est le trou par lequel une garde qui lirait la conclusion du run accepterait un arbre
dont la suite n'a jamais tourné.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_CHEMIN = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "garde_tronc_vert.py"
_spec = importlib.util.spec_from_file_location("garde_tronc_vert", _CHEMIN)
garde = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(garde)


TRONC = ".github/workflows/ci.yml"
SHA = "40227af8eaba5fa47daa85265d96b01178c47bd3"
TAG = "v1.242.0"
SUR_MAIN = "ahead"  # `compare/<sha>...main` : main est en avance, le sha en est un ancêtre


def run(rid, path=TRONC, status="completed", conclusion="success", cree="2026-09-08T08:29:30Z"):
    return {"id": rid, "path": path, "status": status, "conclusion": conclusion, "created_at": cree}


def jobs(**noms):
    """`jobs(test="success", **{"empreinte-collecte": "skipped"})` → la liste de l'API.

    Une conclusion `None` vaut « pas encore conclu » et donne `status: in_progress`,
    comme l'API : c'est ce couple que la garde lit pour distinguer un job qui tourne
    d'un job qui a rendu son verdict.
    """
    return [
        {
            "name": nom,
            "conclusion": etat,
            "status": "in_progress" if etat is None else "completed",
        }
        for nom, etat in noms.items()
    ]


def sans_jobs(_run_id):
    raise AssertionError("les jobs ne devaient pas être consultés")


# ── 1. Le cas nominal : un verdict vert existe, il est réutilisé et NOMMÉ ────────────


def test_un_run_vert_sur_le_sha_est_reutilise():
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [run(34204776194)], lambda _: jobs(test="success"))
    assert verdict.accepte
    # Le succès doit dire QUEL verdict il réutilise : sans l'identifiant, on ne peut pas
    # remonter à ce qui a réellement été éprouvé.
    assert "34204776194" in verdict.message


def test_un_job_saute_a_cote_du_test_vert_ne_gene_pas():
    """Un job de contrat sauté (aucun consommateur lisible) à côté de `test` vert : normal."""
    verdict = garde.juger(
        TAG,
        SHA,
        SUR_MAIN,
        [run(34204776194)],
        lambda _: jobs(test="success", **{"Contrat du front consommateur (avant fusion)": "skipped"}),
    )
    assert verdict.accepte


# ── 2 à 7. Ce que la garde doit REFUSER ─────────────────────────────────────────────


def test_refuse_un_sha_sans_aucun_run():
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [], sans_jobs)
    assert not verdict.accepte
    assert "aucun run" in verdict.message.lower()
    assert SHA in verdict.message


def test_refuse_un_sha_qui_ne_porte_que_des_runs_d_autres_workflows():
    """Un run vert d'un AUTRE workflow (Dependabot, CLA…) n'est pas un verdict de suite."""
    autres = [
        run(1, path=".github/workflows/cla.yml"),
        run(2, path="dynamic/dependabot"),
    ]
    verdict = garde.juger(TAG, SHA, SUR_MAIN, autres, sans_jobs)
    assert not verdict.accepte
    # Le refus doit nommer ce qu'il a vu à la place, sinon il envoie chercher à l'aveugle.
    assert "cla.yml" in verdict.message


def test_refuse_un_run_du_tronc_rouge_et_nomme_sa_conclusion():
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [run(34193576085, conclusion="failure")], sans_jobs)
    assert not verdict.accepte
    assert "34193576085" in verdict.message
    assert "failure" in verdict.message


def test_refuse_un_run_vert_dont_le_job_test_est_saute():
    """LE trou. Conclusion du run `success`, job `test` `skipped` : la suite n'a pas tourné."""
    verdict = garde.juger(
        TAG, SHA, SUR_MAIN, [run(34193576085)], lambda _: jobs(test="skipped", **{"syntaxe-plancher": "success"})
    )
    assert not verdict.accepte, "un run vert dont le job `test` est sauté doit être REFUSÉ"
    assert "34193576085" in verdict.message
    assert "skipped" in verdict.message


def test_refuse_un_run_vert_dont_le_job_test_est_rouge():
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [run(42)], lambda _: jobs(test="failure"))
    assert not verdict.accepte
    assert "failure" in verdict.message


def test_refuse_un_run_vert_sans_aucun_job_nomme_test():
    """Le workflow a pu être réécrit dans ce commit : le job attendu n'existe plus."""
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [run(43)], lambda _: jobs(**{"syntaxe-plancher": "success"}))
    assert not verdict.accepte
    assert "test" in verdict.message
    assert "syntaxe-plancher" in verdict.message


def test_refuse_un_run_encore_en_cours():
    """Un run qui tourne n'est pas un verdict — et le refus doit le distinguer d'une absence."""
    verdict = garde.juger(
        TAG,
        SHA,
        SUR_MAIN,
        [run(44, status="in_progress", conclusion=None)],
        lambda _: jobs(test=None),
    )
    assert not verdict.accepte
    assert "n'a pas conclu" in verdict.message
    assert "tourne ENCORE" in verdict.message


# ── 7 bis. LA TROISIÈME FORME DE REFUS, celle qui ressemble aux autres ──────────────
#
# Rencontrée en vrai le 08/09/2026 sur `324ef83c` : job `test` vert depuis 1 min 30,
# run encore `in_progress` parce que `contrat-front` n'avait pas rendu (15 s de plus).
# Lue vite, elle se comprend « les tests ne sont pas finis » → « j'en ai pour huit
# minutes » → « je contourne ». La vraie réponse était « attends une minute ».
# Le danger n'est pas que la garde se trompe : c'est qu'elle ait raison d'une manière
# qu'on relaie de travers.


def test_un_run_en_vol_dont_la_suite_est_DEJA_verte_le_dit():
    verdict = garde.juger(
        TAG,
        SHA,
        SUR_MAIN,
        [run(34242622982, status="in_progress", conclusion=None)],
        lambda _: [
            {"name": "test", "conclusion": "success", "status": "completed",
             "completed_at": "2026-09-08T15:13:05Z"},
            {"name": "contrat-front", "conclusion": None, "status": "in_progress"},
        ],
    )
    assert not verdict.accepte, "un run non conclu ne vaut pas verdict, même suite verte"
    # Ce que le message DOIT porter pour ne pas être relayé de travers.
    assert "LA SUITE A DÉJÀ PASSÉ" in verdict.message
    assert "contrat-front" in verdict.message, "il faut nommer ce qui reste à conclure"
    assert "dizaines de secondes" in verdict.message
    assert "contourner" in verdict.message


def test_un_run_en_vol_dont_la_suite_est_deja_rouge_ne_fait_pas_attendre():
    """Inutile de faire patienter neuf minutes un run dont le verdict est déjà tombé."""
    verdict = garde.juger(
        TAG,
        SHA,
        SUR_MAIN,
        [run(45, status="in_progress", conclusion=None)],
        lambda _: jobs(test="failure", **{"contrat-front": None}),
    )
    assert not verdict.accepte
    assert "inutile d'attendre" in verdict.message


# ── 8. Plusieurs runs sur le même arbre ─────────────────────────────────────────────


def test_un_vert_plus_ancien_suffit_et_le_message_dit_lequel():
    """L'arbre est immuable : un verdict vert rendu sur lui reste valide.

    Refuser sur un rouge POSTÉRIEUR rejouerait l'incident du 07/09 (un run bloqué par son
    runner bloquait la production alors que l'arbre était sain). Mais le message doit
    nommer le run réutilisé, pour qu'on sache lequel a fait foi.
    """
    runs = [
        run(200, conclusion="failure", cree="2026-09-08T09:00:00Z"),
        run(100, conclusion="success", cree="2026-09-08T08:00:00Z"),
    ]
    verdict = garde.juger(TAG, SHA, SUR_MAIN, runs, lambda _: jobs(test="success"))
    assert verdict.accepte
    assert "100" in verdict.message


def test_tous_les_runs_sont_enumeres_dans_le_refus():
    """Trois runs, trois défauts différents : le refus les nomme tous les trois."""
    runs = [
        run(300, conclusion="failure", cree="2026-09-08T09:00:00Z"),
        run(200, status="in_progress", conclusion=None, cree="2026-09-08T08:30:00Z"),
        run(100, cree="2026-09-08T08:00:00Z"),
    ]
    verdict = garde.juger(TAG, SHA, SUR_MAIN, runs, lambda _: jobs(test="skipped"))
    assert not verdict.accepte
    for identifiant in ("300", "200", "100"):
        assert identifiant in verdict.message


# ── 9. Ne pas savoir, c'est refuser ─────────────────────────────────────────────────


def test_sans_jeton_la_garde_refuse_au_lieu_de_laisser_passer(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "otomata-tech/oto-backend")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert garde.main(["garde", TAG]) == garde.CODE_INJUGEABLE


def test_une_api_muette_refuse(monkeypatch):
    def tombe(*_a, **_k):
        raise garde.Injugeable("réseau coupé")

    monkeypatch.setenv("GITHUB_REPOSITORY", "otomata-tech/oto-backend")
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    monkeypatch.setattr(garde, "_api", tombe)
    assert garde.main(["garde", TAG]) == garde.CODE_INJUGEABLE


def test_le_code_de_sortie_distingue_accepte_et_refus(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "otomata-tech/oto-backend")
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")

    reponses = {
        "/repos/otomata-tech/oto-backend/commits/" + TAG: {"sha": SHA},
        "/repos/otomata-tech/oto-backend/actions/runs?head_sha={}&event=push&branch=main"
        "&per_page=100".format(SHA): {"workflow_runs": [run(34204776194)]},
        "/repos/otomata-tech/oto-backend/compare/{}...main".format(SHA): {"status": "ahead"},
        "/repos/otomata-tech/oto-backend/actions/runs/34204776194/jobs?per_page=100": {
            "jobs": jobs(test="success")
        },
    }
    monkeypatch.setattr(garde, "_api", lambda chemin, _jeton: reponses[chemin])
    assert garde.main(["garde", TAG]) == garde.CODE_ACCEPTE

    reponses[
        "/repos/otomata-tech/oto-backend/actions/runs/34204776194/jobs?per_page=100"
    ] = {"jobs": jobs(test="skipped")}
    assert garde.main(["garde", TAG]) == garde.CODE_REFUS


@pytest.mark.parametrize("champ", ["path", "conclusion"])
def test_la_garde_ne_devine_pas_un_champ_absent(champ):
    """Un run tronqué par l'API ne doit pas être lu comme un succès."""
    incomplet = run(500)
    del incomplet[champ]
    verdict = garde.juger(TAG, SHA, SUR_MAIN, [incomplet], lambda _: jobs(test="success"))
    assert not verdict.accepte


# ── 10. Le sha du tag doit être sur le tronc ────────────────────────────────────────


@pytest.mark.parametrize("position", ["ahead", "identical"])
def test_un_sha_ancetre_ou_pointe_de_main_est_accepte(position):
    verdict = garde.juger(TAG, SHA, position, [run(7)], lambda _: jobs(test="success"))
    assert verdict.accepte


@pytest.mark.parametrize("position", ["behind", "diverged", ""])
def test_refuse_un_sha_hors_du_tronc_meme_avec_un_run_vert(position):
    """Un tag posé sur une branche, ou sur un commit retiré de `main`, ne part pas — même
    si un run vert porte ce sha. La position se lit AVANT les runs, qui ne sont pas lus."""
    verdict = garde.juger(TAG, SHA, position, [run(7)], sans_jobs)
    assert not verdict.accepte
    assert "pas sur `main`" in verdict.message
    assert SHA in verdict.message


def test_le_code_de_sortie_refuse_un_sha_hors_du_tronc(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "otomata-tech/oto-backend")
    monkeypatch.setenv("GITHUB_TOKEN", "jeton-de-test")
    reponses = {
        "/repos/otomata-tech/oto-backend/commits/" + TAG: {"sha": SHA},
        "/repos/otomata-tech/oto-backend/compare/{}...main".format(SHA): {"status": "diverged"},
        "/repos/otomata-tech/oto-backend/actions/runs?head_sha={}&event=push&branch=main"
        "&per_page=100".format(SHA): {"workflow_runs": [run(1)]},
    }
    monkeypatch.setattr(garde, "_api", lambda chemin, _jeton: reponses[chemin])
    assert garde.main(["garde", TAG]) == garde.CODE_REFUS


def test_la_garde_lit_le_workflow_du_tronc_par_son_chemin():
    """Un renommage de `ci.yml` rendrait la garde muette (aucun run trouvé) : le chemin
    qu'elle lit doit exister, et porter le job `test`."""
    import yaml
    racine = pathlib.Path(__file__).resolve().parents[1]
    wf = yaml.safe_load((racine / garde.WORKFLOW_TRONC).read_text())
    assert garde.JOB_EXIGE in wf["jobs"]
