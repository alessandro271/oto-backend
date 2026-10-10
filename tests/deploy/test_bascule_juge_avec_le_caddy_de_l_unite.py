"""La bascule bleu/vert juge le Caddyfile avec le binaire que l'unité `caddy` lance.

Le 10/10/2026, la préproduction a refusé toute bascule : « caddy validate REFUSE la
config — amont restauré ». Le Caddyfile portait depuis la veille une directive d'un
module tiers (`rate_limit`), que le binaire xcaddy de la box (`caddy-custom`) sert
et que `/usr/bin/caddy`, celui du paquet, ne connaît pas. `bg_switch` validait avec
le paquet : un refus pour une config que Caddy servait déjà. Aucun dégât (l'amont est
restauré, l'ancienne couleur sert), mais plus rien ne pouvait se déployer.

Ce banc fige le choix du binaire (`bg_caddy`) : `caddy-custom` s'il existe, sinon
`caddy`, et `BG_CADDY` surcharge les deux ; et le fait que `bg_switch` passe par lui,
jamais par un `caddy` nu.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = RACINE / "deploy" / "oto-mcp-bluegreen.sh"


def _bg_caddy(bins: list[str], env_sup: dict[str, str] | None, tmp: pathlib.Path) -> str:
    """Évalue `bg_caddy` (une ligne du script, extraite telle quelle) avec un PATH qui ne
    contient que les binaires demandés."""
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    for nom in bins:
        f = d / nom
        f.write_text("#!/bin/bash\nexit 0\n")
        f.chmod(0o755)
    ligne = next(l for l in SCRIPT.read_text(encoding="utf-8").splitlines()
                 if l.startswith("bg_caddy()"))
    env = {"PATH": str(d)}
    env.update(env_sup or {})
    r = subprocess.run(["/bin/bash", "-uc", ligne + "\nbg_caddy"], env=env,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def test_le_binaire_xcaddy_prime_quand_il_existe(tmp_path):
    assert _bg_caddy(["caddy", "caddy-custom"], None, tmp_path).endswith("/caddy-custom")


def test_sans_binaire_xcaddy_c_est_le_paquet(tmp_path):
    assert _bg_caddy(["caddy"], None, tmp_path).endswith("/caddy")


def test_bg_caddy_surcharge_les_deux(tmp_path):
    assert _bg_caddy(["caddy", "caddy-custom"], {"BG_CADDY": "/x/mon-caddy"},
                     tmp_path) == "/x/mon-caddy"


def test_bg_switch_juge_avec_ce_binaire_jamais_un_caddy_nu():
    texte = SCRIPT.read_text(encoding="utf-8")
    assert '"$caddy" validate --config "$BG_CADDYFILE"' in texte
    assert re.search(r"(?m)^\s*caddy validate ", texte) is None, (
        "un `caddy validate` nu juge avec le paquet, qui peut refuser une directive "
        "que l'unité sert : passer par bg_caddy")
