"""La garde de mise en production doit REFUSER, pas seulement laisser passer.

Une garde qu'on n'a jamais vue mordre ne prouve rien. Le dépôt en porte déjà l'exemple :
l'étape « Refuser de publier un tronc rouge » de `release.yml` AVERTIT et continue quand
elle ne trouve aucun run — elle n'a donc jamais refusé personne, et personne ne s'en est
aperçu. Ces cas-ci existent pour que celle de `deploy.yml` ne finisse pas décorative.

Le cas central est le §5 : un run dont la CONCLUSION est `success` alors que son job `test`
est `skipped`. Ce n'est pas une hypothèse de laboratoire — c'est l'état de chaque poussée
sur le tronc (le job « Contrat du front consommateur (avant fusion) » y est `skipped`), et
c'est le trou par lequel une garde qui lirait la conclusion du run accepterait un arbre
dont la suite n'a jamais tourné.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_CHEMIN = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "garde_preprod_verte.py"
_spec = importlib.util.spec_from_file_location("garde_preprod_verte", _CHEMIN)
garde = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(garde)


PREPROD = ".github/workflows/deploy-canari.yml"
SHA = "40227af8eaba5fa47daa85265d96b01178c47bd3"
TAG = "v1.242.0"


def run(rid, path=PREPROD, status="completed", conclusion="success", cree="2026-09-08T08:29:30Z"):
    return {"id": rid, "path": path, "status": status, "conclusion": conclusion, "created_at": cree}


def jobs(**noms):
    """`jobs(test="success", **{"deploy-preprod": "skipped"})` → la liste de l'API."""
    return [{"name": nom, "conclusion": etat} for nom, etat in noms.items()]


def sans_jobs(_run_id):
    raise AssertionError("les jobs ne devaient pas être consultés")


# ── 1. Le cas nominal : un verdict vert existe, il est réutilisé et NOMMÉ ────────────


def test_un_run_vert_sur_le_sha_est_reutilise():
    verdict = garde.juger(TAG, SHA, [run(34204776194)], lambda _: jobs(test="success"))
    assert verdict.accepte
    # Le succès doit dire QUEL verdict il réutilise : sans l'identifiant, on ne peut pas
    # remonter à ce qui a réellement été éprouvé.
    assert "34204776194" in verdict.message


def test_un_job_saute_a_cote_du_test_vert_ne_gene_pas():
    """`contrat-front-pr` est `skipped` à chaque poussée sur le tronc : c'est normal."""
    verdict = garde.juger(
        TAG,
        SHA,
        [run(34204776194)],
        lambda _: jobs(test="success", **{"Contrat du front consommateur (avant fusion)": "skipped"}),
    )
    assert verdict.accepte


# ── 2 à 7. Ce que la garde doit REFUSER ─────────────────────────────────────────────


def test_refuse_un_sha_sans_aucun_run():
    verdict = garde.juger(TAG, SHA, [], sans_jobs)
    assert not verdict.accepte
    assert "aucun run" in verdict.message.lower()
    assert SHA in verdict.message


def test_refuse_un_sha_qui_ne_porte_que_des_runs_d_autres_workflows():
    """Un run vert d'un AUTRE workflow (Dependabot, CLA…) n'est pas un verdict de suite."""
    autres = [
        run(1, path=".github/workflows/cla.yml"),
        run(2, path="dynamic/dependabot"),
    ]
    verdict = garde.juger(TAG, SHA, autres, sans_jobs)
    assert not verdict.accepte
    # Le refus doit nommer ce qu'il a vu à la place, sinon il envoie chercher à l'aveugle.
    assert "cla.yml" in verdict.message


def test_refuse_un_run_de_preprod_rouge_et_nomme_sa_conclusion():
    verdict = garde.juger(TAG, SHA, [run(34193576085, conclusion="failure")], sans_jobs)
    assert not verdict.accepte
    assert "34193576085" in verdict.message
    assert "failure" in verdict.message


def test_refuse_un_run_vert_dont_le_job_test_est_saute():
    """LE trou. Conclusion du run `success`, job `test` `skipped` : la suite n'a pas tourné."""
    verdict = garde.juger(
        TAG, SHA, [run(34193576085)], lambda _: jobs(test="skipped", **{"syntaxe-plancher": "success"})
    )
    assert not verdict.accepte, "un run vert dont le job `test` est sauté doit être REFUSÉ"
    assert "34193576085" in verdict.message
    assert "skipped" in verdict.message


def test_refuse_un_run_vert_dont_le_job_test_est_rouge():
    verdict = garde.juger(TAG, SHA, [run(42)], lambda _: jobs(test="failure"))
    assert not verdict.accepte
    assert "failure" in verdict.message


def test_refuse_un_run_vert_sans_aucun_job_nomme_test():
    """Le workflow a pu être réécrit dans ce commit : le job attendu n'existe plus."""
    verdict = garde.juger(TAG, SHA, [run(43)], lambda _: jobs(**{"syntaxe-plancher": "success"}))
    assert not verdict.accepte
    assert "test" in verdict.message
    assert "syntaxe-plancher" in verdict.message


def test_refuse_un_run_encore_en_cours():
    """Un run qui tourne n'est pas un verdict — et le refus doit le distinguer d'une absence."""
    verdict = garde.juger(
        TAG, SHA, [run(44, status="in_progress", conclusion=None)], sans_jobs
    )
    assert not verdict.accepte
    assert "in_progress" in verdict.message


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
    verdict = garde.juger(TAG, SHA, runs, lambda _: jobs(test="success"))
    assert verdict.accepte
    assert "100" in verdict.message


def test_tous_les_runs_sont_enumeres_dans_le_refus():
    """Trois runs, trois défauts différents : le refus les nomme tous les trois."""
    runs = [
        run(300, conclusion="failure", cree="2026-09-08T09:00:00Z"),
        run(200, status="in_progress", conclusion=None, cree="2026-09-08T08:30:00Z"),
        run(100, cree="2026-09-08T08:00:00Z"),
    ]
    verdict = garde.juger(TAG, SHA, runs, lambda _: jobs(test="skipped"))
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
    verdict = garde.juger(TAG, SHA, [incomplet], lambda _: jobs(test="success"))
    assert not verdict.accepte
