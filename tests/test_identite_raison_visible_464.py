"""Une requête ne devient plus anonyme sans dire pourquoi (oto-backend#464).

Treize sites rattrapaient l'échec de `current_user_sub_from_token()` en `sub = None`
(dette « sub avalé », #424 verdict C) : un alias refusé, un compte en pause derrière
un ancien identifiant, une base injoignable — et la requête repartait ANONYME, donc
sans filtrage de catalogue, sans instructions d'org, sans garde de pause, et sans une
ligne au journal. Le seam dit désormais la RAISON, et les sites laissent monter.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import types

import pytest

from oto_mcp.auth import hooks
from oto_mcp.db.sub_aliases import AliasNonResolvable
from oto_mcp.db.users import CompteEnPause
from oto_mcp.mcp_errors import McpError

_RACINE = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"


def _porte(monkeypatch, claims) -> None:
    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token",
                        lambda: types.SimpleNamespace(claims=claims, client_id="c"))


@pytest.fixture
def drain(monkeypatch):
    monkeypatch.setattr(hooks, "alias_drain_armed", lambda: True)


# ── 1. le seam : la raison est journalisée, l'échec n'est pas reclassé ─────────

def test_un_jeton_sans_sub_nest_pas_le_compte_de_dev(monkeypatch, caplog):
    """`OTO_MCP_DEV_SUB` ne vaut que SANS jeton : un jeton vérifié qui n'a pas de
    `sub` est une identité illisible, et la ligne de journal dit laquelle."""
    monkeypatch.setenv("OTO_MCP_DEV_SUB", "sub-dev-de-secours")
    _porte(monkeypatch, {"azp": "client-x", "email": "a@exemple.invalid"})
    with caplog.at_level(logging.WARNING, logger="oto_mcp.auth.hooks"):
        assert hooks.current_user_sub_from_token() is None
    assert "raison=sub_absent" in caplog.text and "client=client-x" in caplog.text


def test_un_alias_refuse_se_leve_avec_sa_raison(monkeypatch, caplog, drain):
    from oto_mcp import db

    def refuse(sub):
        raise AliasNonResolvable(sub, "compte_disparu", "le compte visé n'existe plus")

    monkeypatch.setattr(db, "resolve_sub", refuse)
    _porte(monkeypatch, {"sub": "fantome", "azp": "client-x"})
    with caplog.at_level(logging.WARNING, logger="oto_mcp.auth.hooks"):
        with pytest.raises(AliasNonResolvable):
            hooks.current_user_sub_from_token()
    assert "raison=alias_compte_disparu" in caplog.text
    assert "sub_jeton=fantome" in caplog.text


def test_une_panne_de_base_se_leve_sans_recopier_son_texte(monkeypatch, caplog, drain):
    """Au-dessus de l'enveloppe d'erreur, le SDK amont sert `str(exc)` au client :
    celui d'un pilote de base peut nommer un hôte. Le refus dit quoi faire ; la cause
    reste chaînée pour le journal."""
    from oto_mcp import db

    def tombe(sub):
        raise ConnectionError('connection to server at "10.9.8.7", port 5432 failed')

    monkeypatch.setattr(db, "resolve_sub", tombe)
    _porte(monkeypatch, {"sub": "quelquun"})
    with caplog.at_level(logging.WARNING, logger="oto_mcp.auth.hooks"):
        with pytest.raises(hooks.IdentiteIndisponible) as leve:
            hooks.current_user_sub_from_token()
    assert "10.9.8.7" not in str(leve.value)
    assert isinstance(leve.value.__cause__, ConnectionError)
    assert "raison=base_indisponible:ConnectionError" in caplog.text


# ── 2. les sites : plus aucun ne rattrape l'échec pour repartir anonyme ────────

def _essais_autour_du_seam():
    """(fichier, ligne, source du handler) de chaque `try` dont le corps appelle le
    seam et qui porte un handler LARGE."""
    for chemin in sorted(_RACINE.rglob("*.py")):
        src = chemin.read_text(encoding="utf-8")
        lignes = src.splitlines()
        for noeud in ast.walk(ast.parse(src)):
            if not isinstance(noeud, ast.Try):
                continue
            appelle = any(
                isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None))
                == "current_user_sub_from_token"
                for b in noeud.body for n in ast.walk(b))
            if not appelle:
                continue
            for h in noeud.handlers:
                if h.type is None or ast.unparse(h.type) in ("Exception", "BaseException"):
                    autour = "\n".join(lignes[max(0, h.lineno - 3):h.lineno])
                    yield chemin.relative_to(_RACINE), h.lineno, autour


def test_aucun_site_ne_rattrape_le_seam_en_silence():
    """Un `except Exception` autour du seam annoté `noqa: SILENT` EST le motif de
    #464 : l'échec d'identité reclassé en « pas de jeton ». Survivants permis, parce
    qu'aucun ne décide de l'identité SOUS LAQUELLE la requête est servie — ils
    tournent après elle, ou dans le traitement d'une AUTRE erreur qu'un échec
    d'identité masquerait :
    - `sentry_setup.py` — la capture d'une erreur d'outil ;
    - `server.py` — l'enrichissement du journal APRÈS l'appel (dette à part,
      « l'enrichissement tombe d'un bloc », famille suivante de #424) ;
    - `tools/datastore.py` — l'indice ajouté à un refus de ligne réservée."""
    permis = {"sentry_setup.py", "server.py", "tools/datastore.py"}
    fautifs = [f"{f}:{ligne}" for f, ligne, autour in _essais_autour_du_seam()
               if "noqa: SILENT" in autour and str(f) not in permis]
    assert not fautifs, (
        "l'échec d'identité est rattrapé en silence (la requête repart anonyme) :\n  "
        + "\n  ".join(fautifs)
        + "\nLaisser monter : le seam journalise la raison (#464).")


def test_la_dette_sub_avale_est_soldee():
    restes = [str(p.relative_to(_RACINE)) for p in _RACINE.rglob("*.py")
              if "sub avalé" in p.read_text(encoding="utf-8")]
    assert not restes, f"annotations « sub avalé » restantes : {restes}"


class _Ctx:
    fastmcp_context = None


def test_la_pause_derriere_un_ancien_identifiant_est_refusee_au_contrat(monkeypatch):
    """L'ancien identifiant d'un compte en pause n'a plus de ligne à lui : c'est
    `upsert_user` qui reconnaît la pause (`CompteEnPause`). Le middleware la rendait
    ANONYME ; il la refuse avec le code que les deux faces servent."""
    from oto_mcp.middleware import account_suspended as mod

    def seam():
        raise CompteEnPause("u-canonique", "départ", "l'identifiant u-ancien y redirige")

    monkeypatch.setattr(mod, "current_user_sub_from_token", seam)

    async def _next(ctx):
        raise AssertionError("la chaîne ne doit pas tourner")

    with pytest.raises(McpError) as leve:
        asyncio.run(mod.AccountSuspendedMiddleware().on_request(_Ctx(), _next))
    assert leve.value.error.code == -32003
    assert leve.value.error.data["code"] == "account_suspended"
    assert "en pause" in leve.value.error.message


@pytest.mark.parametrize("hook", ["on_initialize"])
def test_le_handshake_ne_sert_pas_un_catalogue_anonyme(monkeypatch, hook):
    """Sans identité lisible, le filtrage per-user ne s'applique pas : rattraper
    l'échec servait le catalogue NON FILTRÉ d'une session sans compte."""
    from oto_mcp.middleware import disabled_tools as mod

    def seam():
        raise hooks.IdentiteIndisponible("base_indisponible:OperationalError")

    monkeypatch.setattr(mod, "current_user_sub_from_token", seam)

    async def _next(ctx):
        return "résultat"

    with pytest.raises(hooks.IdentiteIndisponible):
        asyncio.run(getattr(mod.UserDisabledToolsMiddleware(), hook)(_Ctx(), _next))
