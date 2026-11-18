"""Un refus du pont des recettes laisse une trace (#1822) : le code d'une recette reçoit
`{ok: false, code}` comme une valeur et peut « réussir » quand même ; sans trace, une
recette qui butait sur `unknown_tool` ne se voyait nulle part. Le refus est compté sous
`code:outil` (le nom DEMANDÉ) dans le relevé de l'appel MCP qui joue la recette, que
`server._TRACED_ARGS` verse dans sa ligne `tool_calls`.

Sans base : le catalogue est le vrai montage (`static_mcp`)."""
from __future__ import annotations

import asyncio

import pytest

from _mcp_app import static_mcp
from oto_mcp import server, session_org
from oto_mcp.recipes import pont

SUB = "sub-recettes-refus"


@pytest.fixture(scope="module")
def catalogue():
    return static_mcp()


def _sous_releve(coro):
    """Joue `coro` sous le relevé d'un appel MCP ; rend (réponse, relevé)."""
    async def jouer():
        releve: dict = {}
        jeton = session_org.set_call_trace(releve)
        try:
            return await coro(), releve
        finally:
            session_org.reset_call_trace(jeton)
    return asyncio.run(jouer())


def test_la_cle_des_refus_rejoint_la_ligne_du_journal():
    assert pont.CLE_REFUS in server._TRACED_ARGS


@pytest.mark.parametrize("appeleur", ["lecture", "recette"])
def test_un_outil_inconnu_est_compte_une_fois_par_appel(catalogue, appeleur):
    appeler = (pont.appeleur_en_lecture(catalogue, SUB) if appeleur == "lecture" else
               pont.appeleur_de_recette(catalogue, SUB, datastore=None, ecrire=True))

    async def deux_appels():
        return [await appeler("outil_qui_n_existe_pas", {}) for _ in range(2)]

    reponses, releve = _sous_releve(deux_appels)
    assert all(r["ok"] is False and r["code"] == "unknown_tool" for r in reponses)
    # Compté au bord, une fois par appel : la lecture qu'appelle la recette n'en
    # ajoute pas un second.
    assert releve[pont.CLE_REFUS] == {"unknown_tool:outil_qui_n_existe_pas": 2}


def test_les_couples_distincts_sont_bornes(catalogue):
    appeler = pont.appeleur_en_lecture(catalogue, SUB)

    async def beaucoup():
        for i in range(pont.MAX_REFUS_DISTINCTS + 5):
            await appeler(f"inconnu_{i}", {})
        return await appeler("inconnu_0", {})

    _, releve = _sous_releve(beaucoup)
    refus = releve[pont.CLE_REFUS]
    assert len(refus) == pont.MAX_REFUS_DISTINCTS
    assert refus["unknown_tool:inconnu_0"] == 2      # un couple déjà vu compte encore


def test_hors_appel_mcp_rien_n_est_ecrit(catalogue):
    appeler = pont.appeleur_en_lecture(catalogue, SUB)
    reponse = asyncio.run(appeler("outil_qui_n_existe_pas", {}))
    assert reponse["code"] == "unknown_tool"
    assert session_org.current_call_trace() is None
