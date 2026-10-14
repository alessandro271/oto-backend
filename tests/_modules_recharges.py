"""Retirer ou recharger un module le temps d'un test — `sys.modules` ET le paquet parent.

Un test qui retire `oto_mcp.tools.x` de `sys.modules` puis le réimporte crée un
SECOND module. L'import pose aussi l'attribut `x` du paquet `oto_mcp.tools`. Si l'on
ne rend que `sys.modules` à la fin du test (`monkeypatch.delitem`), le paquet garde le
module rechargé pendant que `sys.modules` rend l'ancien : un test suivant qui patche
`from oto_mcp.tools import x` (par le paquet) patche alors un AUTRE module que celui
qu'importe le code servi (`from .x import …`, résolu par `sys.modules`). Le patch ne
mord plus, et le test tombe sur le vrai code — base, réseau.

Vécu le 13/10/2026 (oto-backend #1240) : la fixture `data_app` de
`tests/datastore/test_datastore_app_v2.py` laissait `oto_mcp.tools.datastore`
rechargé dans le paquet ; `test_datastore_table_app`, joué après elle sur le même
worker, atteignait la vraie base (« Missing env var DATABASE_URL »). Rouge seulement
quand la répartition des workers les rapprochait : ajouter un fichier de test
suffisait à le faire apparaître ou disparaître.

Ces deux aides passent par `monkeypatch` pour les DEUX entrées : tout est rendu à la
fin du test. `tests/test_modules_recharges_garde.py` refuse toute autre façon de
retirer une entrée de `sys.modules` dans `tests/`.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


def oublier_module(monkeypatch: pytest.MonkeyPatch, nom: str) -> None:
    """Retire `nom` de `sys.modules` et du paquet parent, rendus à la fin du test.

    Pour simuler un module absent ou forcer un import neuf. Sans effet sur ce qui
    n'est pas chargé."""
    monkeypatch.delitem(sys.modules, nom, raising=False)
    parent, _, feuille = nom.rpartition(".")
    paquet = sys.modules.get(parent) if parent else None
    if paquet is not None:
        monkeypatch.delattr(paquet, feuille, raising=False)


def recharger_module(monkeypatch: pytest.MonkeyPatch, nom: str) -> ModuleType:
    """Réimporte `nom` à neuf (après un stub, une variable d'environnement…) et le rend.

    `sys.modules` et l'attribut du paquet retrouvent l'ancien module à la fin du test."""
    oublier_module(monkeypatch, nom)
    return importlib.import_module(nom)
