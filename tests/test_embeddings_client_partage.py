"""L'embedding de requête (`embed_query`, chemin de `/api/me/search`) réutilise UN client.

Un `httpx.AsyncClient` neuf à chaque recherche payait une poignée de main TLS par appel
vers le fournisseur d'embeddings. Le client est désormais partagé, créé à la première
requête, un par boucle d'événements (un client lie ses connexions à la boucle qui l'a
servi). Transport bouchon : aucun appel réseau.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from oto_mcp import embeddings


@pytest.fixture()
def bouchon(monkeypatch):
    """Un transport qui répond un vecteur, et le relevé des clients construits."""
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    monkeypatch.setattr(embeddings, "_CLIENTS", type(embeddings._CLIENTS)())
    requetes, construits = [], []

    def _repondre(req: httpx.Request) -> httpx.Response:
        requetes.append(req)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.25]}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 0,
                                                   "total_tokens": 3}})

    def _nouveau():
        c = httpx.AsyncClient(transport=httpx.MockTransport(_repondre),
                              timeout=embeddings._DELAI_REQUETE_S)
        construits.append(c)
        return c

    monkeypatch.setattr(embeddings, "_nouveau_client", _nouveau)
    # Le compte du jour, en base ailleurs : ici un compteur en mémoire.
    from oto_mcp.db import embed_file
    compte = {"tokens": 0, "plafond_atteint": False}
    monkeypatch.setattr(embed_file, "jour", lambda: dict(compte))
    monkeypatch.setattr(embed_file, "ajouter_tokens",
                        lambda n: compte.update(tokens=compte["tokens"] + n))
    monkeypatch.delenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", raising=False)
    return requetes, construits, compte


def test_deux_appels_reutilisent_le_meme_client(bouchon):
    requetes, construits, _ = bouchon

    async def _deux():
        a = await embeddings.embed_query("premier")
        b = await embeddings.embed_query("second")
        await embeddings.fermer_client()
        return a, b

    assert asyncio.run(_deux()) == ([0.5, 0.25], [0.5, 0.25])
    assert len(requetes) == 2
    assert len(construits) == 1, f"{len(construits)} clients pour deux appels"
    assert construits[0].is_closed, "fermer_client ferme le client partagé"
    # Même délai qu'avant (8 s), posé sur la requête elle-même.
    assert all(r.extensions["timeout"]["read"] == 8.0 for r in requetes)


def test_un_client_par_boucle(bouchon):
    """Deux boucles (tests, processus annexes) ne se passent jamais leur client."""
    _, construits, _ = bouchon
    asyncio.run(embeddings.embed_query("boucle une"))
    asyncio.run(embeddings.embed_query("boucle deux"))
    assert len(construits) == 2


def test_l_echec_retombe_sur_le_lexical(monkeypatch, bouchon):
    """Même gestion d'erreur : un 5xx rend None, jamais une exception."""

    def _panne():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(503)))

    monkeypatch.setattr(embeddings, "_nouveau_client", _panne)
    assert asyncio.run(embeddings.embed_query("texte")) is None


def test_l_import_ne_cree_aucun_client():
    import importlib
    mod = importlib.reload(embeddings)
    assert len(mod._CLIENTS) == 0


def test_les_tokens_de_la_recherche_comptent_contre_le_plafond(bouchon):
    _, _, compte = bouchon
    asyncio.run(embeddings.embed_query("une recherche"))
    assert compte["tokens"] == 3


def test_au_plafond_la_recherche_passe_en_lexical_et_le_dit(bouchon, monkeypatch, caplog):
    requetes, _, compte = bouchon
    compte["plafond_atteint"] = True
    monkeypatch.setattr(embeddings, "_PLAFOND_DIT", set())
    with caplog.at_level("WARNING", logger="oto_mcp.embeddings"):
        assert asyncio.run(embeddings.embed_query("une recherche")) is None
        assert asyncio.run(embeddings.embed_query("une autre")) is None
    assert requetes == []
    dits = [r for r in caplog.records if "plafond journalier" in r.getMessage()]
    assert len(dits) == 1, "le repli se dit, une fois par jour"
