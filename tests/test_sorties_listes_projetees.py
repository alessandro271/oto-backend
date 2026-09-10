"""Cliquet : un outil NEUF qui rend une liste déclare ce que son défaut retire.

Ce que rend un outil est le poste dominant d'un workflow agentique. Mesuré le
2026-09-10 sur trois jours d'une org cliente en production (`tool_calls.result_size`) :
un seul outil de liste pesait 68 % de tous les caractères servis. Il projetait déjà,
mais seulement des blocs entiers, et il a fallu une PR après coup (#918) pour que son
défaut cesse de rendre ce qu'aucun appelant ne lit : −46 % par enregistrement.

La leçon qui fonde le cliquet est écrite dans `output_projection.py` : **une économie
qu'il faut connaître pour en bénéficier ne bénéficie à personne**. Un agent ne passe
pas un opt-in qu'il ne connaît pas. L'économie doit donc être dans le DÉFAUT de
l'outil — et le seul moment où ça ne coûte rien de l'y mettre, c'est quand l'outil
s'écrit.

**Rattrapage progressif, pas de grand soir** — même patron que
`test_capability_outputs.py` : les 136 outils qui n'ont rien sont nommés dans
`tool_output_debt.txt`. La liste ne peut que rétrécir ; un outil hors liste doit
projeter, sinon la CI casse. Ce test ne corrige rien de l'existant : il empêche le
prochain.

Ce que la règle mesure et ne mesure pas est écrit en tête du fichier de dette, pour
que personne ne lui fasse dire plus qu'elle ne dit.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

_DEBT_FILE = pathlib.Path(__file__).resolve().parent / "tool_output_debt.txt"

# Un de ces paramètres ⟹ l'outil rend une liste par construction.
_PAGINATION = frozenset({"page", "size", "limit", "cursor", "page_size", "per_page",
                         "offset"})

# Un de ces paramètres ⟹ l'appelant peut projeter, et le défaut est la vue resserrée
# (`full=True` rend le brut, ADR 0047).
_PROJECTION_PARAMS = frozenset({"fields", "full"})

# Un de ces appels dans le CORPS de l'outil ⟹ il retire quelque chose avant de rendre.
_PROJECTION_APPELS = ("output_projection", "_shape(", "summarize(", "_project(",
                      "_slim")

# Plafond de la dette : il ne peut que BAISSER. Mesuré le 2026-09-10.
_PLAFOND = 136


def _debt() -> set[str]:
    lines = _DEBT_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def _servis():
    """Les outils du montage RÉEL — un garde-fou d'inventaire qui s'exerce sur une
    fixture partielle ment par omission (`docs/conventions.md`)."""
    from fastmcp import FastMCP

    from oto_mcp.tools import register_all

    m = FastMCP("cliquet-sorties")
    try:
        register_all(m)
    except Exception as e:  # deps optionnelles absentes en CI minimal
        pytest.skip(f"register_all indisponible : {e}")
    return asyncio.run(m.list_tools(run_middleware=False))


def _pagine(tool) -> bool:
    return bool(_PAGINATION & set((tool.parameters or {}).get("properties", {})))


def _projette(tool) -> bool:
    if _PROJECTION_PARAMS & set((tool.parameters or {}).get("properties", {})):
        return True
    try:
        corps = inspect.getsource(tool.fn)
    except (OSError, TypeError):
        # Source illisible (outil généré) : on ne peut pas prouver qu'il projette.
        return False
    return any(appel in corps for appel in _PROJECTION_APPELS)


def _sans_projection(tools) -> set[str]:
    return {t.name for t in tools if _pagine(t) and not _projette(t)}


def test_un_outil_neuf_qui_pagine_declare_sa_projection():
    intrus = sorted(_sans_projection(_servis()) - _debt())
    assert not intrus, (
        f"Outils de LISTE sans projection, hors dette connue : {intrus}. Chacun rend "
        "chaque enregistrement tel que l'amont le livre, et un agent paie ce poids à "
        "chaque tour. Deux issues : exposer `full: bool = False` et resserrer le "
        "défaut (cf. `tools/aiark.py::_shape`), ou appeler `output_projection.project` "
        "/ `summarize` dans le corps de l'outil. Ne PAS l'ajouter à "
        "`tool_output_debt.txt` : la dette ne grossit pas.")


def test_la_dette_ne_ment_pas():
    """Une ligne PAYÉE doit quitter la liste — sinon la marge libérée se remplirait en
    silence, et la liste cesserait de décrire le réel.

    ⚠️ Seuls les outils SERVIS ICI sont jugés. Le catalogue dépend des extras installés
    (un connecteur dont le cœur oto-core manque n'est pas monté, cf. `docs/commands.md`
    §Pin oto-core) : un poste de dev en retard sur le pin en voit moins que la CI. Une
    ligne dont l'outil n'est pas monté ici n'est donc NI « disparue » NI « payée » —
    la juger rendrait ce test rouge sur tout venv en retard, pour une raison qui n'a
    rien à voir avec la dette. La garantie de décroissance est portée par `_PLAFOND`,
    qui ne dépend d'aucun catalogue."""
    tools = _servis()
    servis = {t.name for t in tools}
    payes = sorted((_debt() & servis) - _sans_projection(tools))
    assert not payes, (
        f"Ces outils projettent désormais, ou ne paginent plus : {payes}. Retire-les "
        f"de {_DEBT_FILE.name} ET baisse `_PLAFOND` d'autant — c'est ce qui rend la "
        "baisse définitive.")


def test_la_dette_ne_fait_que_baisser():
    assert len(_debt()) <= _PLAFOND, (
        f"la dette de sortie a grossi ({len(_debt())} outils pour un plafond de "
        f"{_PLAFOND}). Elle doit DÉCROÎTRE : projette plutôt que d'élargir la liste.")
