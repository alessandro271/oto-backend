"""Ce que le CLIENT reçoit au `initialize` — pas ce que le middleware croit avoir posé.

fastmcp laisse la session MCP RÉPONDRE au `initialize` (réponse construite depuis
`session._init_options`) AVANT de rendre la main au middleware (`call_original_handler`,
`fastmcp/server/low_level.py`). Modifier `result` après `call_next` ne change donc rien
à ce qui part : le client recevait la surface STATIQUE du démarrage, jamais le socle
composé par compte, ni celui d'un tenant, ni les readmes, ni le nom du produit dans
`serverInfo`. Les tests unitaires de `test_dynamic_instructions.py` appellent
`on_initialize` avec un `call_next` factice — ils éprouvent le middleware, pas le fil.

Ici, un vrai `fastmcp.Client` se connecte à un serveur qui porte les VRAIS middlewares ;
seuls les seams qui lisent la base sont simulés. Chaque assertion porte sur
`client.initialize_result`, c'est-à-dire sur ce qui a traversé le protocole.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest
from fastmcp import Client, FastMCP

from oto_mcp import tool_alias
from oto_mcp.middleware import _handshake
from oto_mcp.middleware import alias as alias_mw
from oto_mcp.middleware import dynamic_instructions as di

STATIQUE = "SOCLE STATIQUE DU DÉMARRAGE"

# L'identité de la session simulée. Une ContextVar, pas une constante : les tâches de
# la session serveur naissent dans le contexte du client qui se connecte, donc deux
# connexions concurrentes portent chacune la leur — c'est ce qui rend le test de fuite
# entre sessions possible.
_SUB: contextvars.ContextVar[str | None] = contextvars.ContextVar("sub", default=None)


def _serveur() -> FastMCP:
    mcp = FastMCP("oto", instructions=STATIQUE)
    # Même ordre relatif que `server.py` : l'alias avant les instructions dynamiques.
    mcp.add_middleware(alias_mw.ToolAliasMiddleware())
    mcp.add_middleware(di.DynamicInstructionsMiddleware())

    @mcp.tool
    def ping() -> str:
        return "pong"

    return mcp


@pytest.fixture
def seams(monkeypatch):
    """Les seams DB simulés : identité, projet publié, composition, identité produit."""
    etat = {"projet": di._PAS_DE_PROJET_PUBLIE, "compose": lambda sub: f"SOCLE DE {sub}",
            "identite": lambda sub: (None, None)}
    lire = lambda: _SUB.get()
    monkeypatch.setattr(di, "current_user_sub_from_token", lire)
    monkeypatch.setattr(alias_mw, "current_user_sub_from_token", lire)
    monkeypatch.setattr(di, "_published_project_instructions", lambda: etat["projet"])
    monkeypatch.setattr(di, "_session_instructions", lambda sub: etat["compose"](sub))
    monkeypatch.setattr(tool_alias, "server_identity_for", lambda sub: etat["identite"](sub))
    return etat


async def _handshake_de(sub: str | None):
    _SUB.set(sub)
    async with Client(_serveur()) as c:
        return c.initialize_result


def _init(sub: str | None):
    return asyncio.run(_handshake_de(sub))


# ── instructions ─────────────────────────────────────────────────────────────

def test_le_client_recoit_le_socle_COMPOSE_pour_son_compte(seams):
    assert _init("u-1").instructions == "SOCLE DE u-1"


def test_le_socle_d_un_tenant_arrive_au_client(seams):
    """Le cas qui a fait trouver le défaut : un socle de tenant écrit, lu par
    `_socle_for`, et jamais reçu — le client voyait toujours le socle plateforme."""
    seams["compose"] = lambda sub: "Acme — TA boîte à outils… `acme_guide`"
    recu = _init("acme:u-1").instructions
    assert recu.startswith("Acme — ") and "acme_guide" in recu


def test_deux_sessions_concurrentes_ne_se_voient_pas(seams):
    """La surface est posée sur la SESSION, jamais sur le serveur partagé."""
    async def deux():
        return await asyncio.gather(_handshake_de("u-1"), _handshake_de("u-2"))
    a, b = asyncio.run(deux())
    assert (a.instructions, b.instructions) == ("SOCLE DE u-1", "SOCLE DE u-2")


def test_une_session_suivante_ne_herite_pas_de_la_precedente(seams):
    _init("u-1")
    assert _init(None).instructions == STATIQUE


def test_un_projet_publie_recoit_sa_prose(seams):
    seams["projet"] = "PROSE DU PROJET"
    assert _init(None).instructions == "PROSE DU PROJET"


def test_un_projet_publie_sans_prose_garde_le_statique(seams):
    seams["projet"] = None
    assert _init("u-1").instructions == STATIQUE


def test_sans_compte_le_statique(seams):
    assert _init(None).instructions == STATIQUE


def test_une_composition_qui_casse_laisse_le_statique_et_le_handshake_passe(seams):
    def boom(sub):
        raise RuntimeError("base indisponible")
    seams["compose"] = boom
    assert _init("u-1").instructions == STATIQUE


def test_une_composition_lente_laisse_le_statique_dans_le_temps_borne(seams, monkeypatch):
    """La réponse au `initialize` attend la composition : au-delà de la borne, la
    surface statique part — le client n'attend jamais une cascade lente."""
    import time
    monkeypatch.setattr(di, "BORNE_COMPOSITION_S", 0.2)

    def lente(sub):
        time.sleep(1.5)
        return "TROP TARD"
    seams["compose"] = lente
    t0 = time.monotonic()
    assert _init("u-1").instructions == STATIQUE
    assert time.monotonic() - t0 < 1.2


def test_un_projet_publie_lent_laisse_le_statique_dans_le_temps_borne(seams, monkeypatch):
    import time
    monkeypatch.setattr(di, "BORNE_COMPOSITION_S", 0.2)

    def lent():
        time.sleep(1.5)
        return "PROSE TROP TARD"
    monkeypatch.setattr(di, "_published_project_instructions", lent)
    t0 = time.monotonic()
    assert _init("u-1").instructions == STATIQUE
    assert time.monotonic() - t0 < 1.2


# ── serverInfo ───────────────────────────────────────────────────────────────

def test_le_nom_du_produit_arrive_dans_server_info(seams):
    seams["identite"] = lambda sub: ("acme", "Acme")
    assert _init("acme:u-1").serverInfo.name == "acme"


def test_sans_prefixe_le_nom_d_avant(seams):
    assert _init("u-1").serverInfo.name == "oto"


# ── le seam privé ────────────────────────────────────────────────────────────

def test_le_seam_prive_de_fastmcp_est_toujours_la(seams):
    """`poser_avant_reponse` écrit `session._init_options`, champ privé de la session
    MCP que fastmcp lit pour répondre. Une montée de version qui le déplace ferait
    retomber TOUT le monde sur le statique en silence (fail-open) : ce test le dit."""
    vu = {}

    class Sonde(di.DynamicInstructionsMiddleware):
        async def on_initialize(self, context, call_next):
            vu["pose"] = _handshake.poser_avant_reponse(context, instructions="SONDE")
            return await call_next(context)

    async def go():
        mcp = FastMCP("oto", instructions=STATIQUE)
        mcp.add_middleware(Sonde())
        async with Client(mcp) as c:
            return c.initialize_result
    assert asyncio.run(go()).instructions == "SONDE" and vu["pose"] is True
