"""Le journal d'ACCÈS d'uvicorn n'écrit jamais en clair un jeton porté par le chemin.

#558 a fermé la fuite dans `tool_calls` ; le journal d'accès d'uvicorn (journald,
200 Mo de rétention) recopiait pourtant toujours la cible brute de la requête —
`/api/receivers/apollo/phones/<jeton>`, `/api/upload/<jeton>`… Ces tests gardent la
même propriété sur ce canal : un segment lié à un paramètre de route secret devient
son masque, pour toute route présente ou future qui le déclare.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from oto_mcp import journal_secrets as js
from oto_mcp.auth import relay

JETON = "k3Yq9-J8vTzL0mW2xRpA7bC4dE6fG1hI5jK8lM0nO2p"   # 43 car., la forme émise

# La ligne telle qu'uvicorn la formate (`uvicorn.logging.AccessFormatter`).
FORMAT = '%s - "%s %s HTTP/%s" %d'


@pytest.fixture(autouse=True)
def _table_de_routes_reelle():
    """Déclare la VRAIE table de routes servie — pas une liste fabriquée ici."""
    from oto_mcp.api import routes as api_routes
    api_routes.make_routes(object(), mcp_instance=None)
    yield


@pytest.fixture
def acces(caplog):
    """Un logger monté comme `uvicorn.access` en prod : les deux filtres de `server.py`."""
    lg = logging.getLogger("test.acces.chemin")
    lg.filters.clear()
    lg.addFilter(relay.FiltreJournalAcces())
    lg.addFilter(js.MasqueCheminAcces())

    def ecrire(methode: str, cible: str, statut: int = 200) -> str:
        with caplog.at_level("INFO", logger=lg.name):
            caplog.clear()
            lg.info(FORMAT, "1.2.3.4:5", methode, cible, "1.1", statut)
        return caplog.records[-1].getMessage()
    return ecrire


@pytest.mark.parametrize("gabarit", [
    "/api/receivers/apollo/phones/{}",
    "/api/upload/{}",
    "/api/invitations/{}",
    "/api/public/docs/{}",
    "/p/d/{}",
    "/o/u/{}",
    "/o/d/{}",
])
def test_un_jeton_du_chemin_ne_part_pas_en_clair(acces, gabarit):
    ligne = acces("POST", gabarit.format(JETON))
    assert JETON not in ligne
    assert js.mask(JETON) in ligne, "le masque corrélable remplace le jeton"


ADRESSE_PRIVEE = "h_Zq3xV9mK2pL7wR4tY8uN1bC6"   # la forme `h_…` servie en `hook_url`


@pytest.mark.parametrize("adresse", [ADRESSE_PRIVEE, "4242"])
def test_l_adresse_d_un_webhook_d_agent_ne_part_pas_en_clair(acces, adresse):
    """`/api/hooks/{address}` : l'adresse privée est ce qui rend l'agent introuvable ;
    l'id numérique d'un agent sans adresse privée passe par le même segment, que la
    route ne distingue qu'en lisant la base — masqué pareil."""
    ligne = acces("POST", f"/api/hooks/{adresse}", 202)
    assert f"/api/hooks/{adresse}" not in ligne
    assert js.mask(adresse) in ligne


def test_l_adresse_d_un_webhook_ne_part_pas_en_clair_dans_tool_calls():
    """L'autre canal : la ligne `tool_calls` d'un POST de webhook (`route_and_secrets`,
    lu par le journal REST) ne porte l'adresse ni dans `tool` ni en clair dans `args`."""
    route, masques = js.route_and_secrets(f"/api/hooks/{ADRESSE_PRIVEE}")
    assert route == "/api/hooks/:address"
    assert masques == {"address": js.mask(ADRESSE_PRIVEE)}


def test_aucune_capacite_ne_porte_un_argument_nomme_comme_un_secret_de_route():
    """Un nom de `SECRET_PARAM_NAMES` masque aussi l'argument de CAPACITÉ qui le porte :
    `address` n'en touche aucun aujourd'hui — un ajout futur doit être voulu."""
    import oto_mcp.capabilities  # noqa: F401 — peuple le registre
    from oto_mcp.capabilities.registry import caps_with_mcp
    porteurs = sorted(c.mcp for c in caps_with_mcp()
                      if "address" in (getattr(c.Input, "model_fields", {}) or {}))
    assert porteurs == []


def test_la_requete_et_la_queue_restent_lisibles(acces):
    ligne = acces("GET", f"/api/upload/{JETON}/x?format=csv", 404)
    assert JETON not in ligne and "/x?format=csv" in ligne


def test_une_route_ordinaire_est_recopiee_telle_quelle(acces):
    assert "/api/datastores/204/rows?limit=5" in acces("GET", "/api/datastores/204/rows?limit=5")


def test_le_filtre_du_relais_oauth_tient_toujours(acces):
    ligne = acces("GET", "/oauth/callback?code=SECRET&state=SCEAU&iss=https%3A%2F%2Fa", 302)
    assert "SECRET" not in ligne and "SCEAU" not in ligne and "iss=" in ligne


def test_une_ligne_hors_forme_passe_sans_lever(caplog):
    lg = logging.getLogger("test.acces.hors_forme")
    lg.addFilter(js.MasqueCheminAcces())
    with caplog.at_level("INFO", logger=lg.name):
        lg.info("sans arguments")
        lg.info("%s", 42)
    assert [r.getMessage() for r in caplog.records] == ["sans arguments", "42"]


def test_le_serveur_monte_le_filtre_sur_uvicorn_access():
    """Cliquet : sans ce branchement, tout ce qui précède est inerte EN SILENCE."""
    source = (Path(__file__).resolve().parent.parent / "oto_mcp" / "server.py").read_text()
    assert 'getLogger("uvicorn.access").addFilter(journal_secrets.MasqueCheminAcces())' in source
