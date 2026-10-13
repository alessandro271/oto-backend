"""`oto-mcp maintenance cles-plateforme-au-format` : les clés plateforme posées BRUTES
d'un connecteur multi-champs sont réécrites au format du coffre, une fois.

Avant que `/api/admin/platform-keys` passe par `preparer_pose`, une clé `forager` était
stockée telle que collée, et `unpack_secret` ne la relisait pas. La commande est À BLANC
par défaut, ne réécrit rien si une ligne est illisible, ne touche que le chiffré, et ne
rend jamais une valeur.
"""
from __future__ import annotations

import json
import os

import pytest

from _base_jetable import base_jetable


@pytest.fixture
def base(pg_dsn, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    with base_jetable(pg_dsn):
        from oto_mcp.db import init_db
        init_db()
        yield


def _poser_brute(label: str, connector: str, valeur: str) -> None:
    """La pose d'AVANT : la valeur collée, sans `preparer_pose`."""
    from oto_mcp import credentials_store
    credentials_store.set_platform_key(label, connector, valeur, set_by="op")


def _ligne(label: str, connector: str) -> dict:
    from oto_mcp import credentials_store
    from oto_mcp.db import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT version, set_by, set_at, meta, share_mode FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (credentials_store.PLATFORM, label, connector)).fetchone()
    clair = credentials_store.get_credential(credentials_store.PLATFORM, label, connector)
    return {**r, "clair": clair}


def _verdicts(out: dict) -> dict:
    return {(c["connector"], c["label"]): c["verdict"] for c in out["cles"]}


def test_a_blanc_dit_ce_qu_il_reecrirait_sans_rien_ecrire(base):
    from oto_mcp import cles_plateforme_format as f
    _poser_brute("oto", "forager", "fg-brute")
    avant = _ligne("oto", "forager")

    out = f.remettre_au_format(dry_run=True)

    assert out["applied"] is False
    assert _verdicts(out) == {("forager", "oto"): f.A_REECRIRE}
    assert _ligne("oto", "forager") == avant
    assert "fg-brute" not in json.dumps(out, default=str), "jamais une valeur au rapport"


def test_la_cle_brute_est_reecrite_au_format_et_seul_le_chiffre_change(base):
    from oto_mcp import credentials_store
    from oto_mcp import cles_plateforme_format as f
    _poser_brute("oto", "forager", "fg-brute")
    avant = _ligne("oto", "forager")

    out = f.remettre_au_format(dry_run=False)

    assert _verdicts(out) == {("forager", "oto"): f.REECRITE}
    apres = _ligne("oto", "forager")
    assert credentials_store.unpack_secret("forager", apres["clair"]) == {"api_key": "fg-brute"}
    assert apres["version"] == avant["version"] + 1
    for col in ("set_by", "set_at", "meta", "share_mode"):
        assert apres[col] == avant[col], col


def test_rejouer_ne_touche_plus_rien(base):
    from oto_mcp import cles_plateforme_format as f
    _poser_brute("oto", "forager", "fg-brute")
    f.remettre_au_format(dry_run=False)
    version = _ligne("oto", "forager")["version"]

    out = f.remettre_au_format(dry_run=False)

    assert _verdicts(out) == {("forager", "oto"): f.AU_FORMAT}
    assert _ligne("oto", "forager")["version"] == version


def test_une_cle_deja_au_format_et_un_mono_champ_ne_sont_pas_touches(base):
    from oto_mcp import credentials_store
    from oto_mcp import cles_plateforme_format as f
    credentials_store.set_platform_key(
        "oto", "transcription", credentials_store.pack_secret(
            "transcription", {"api_key": "tr-1", "language": "fr"}), set_by="op")
    credentials_store.set_platform_key("oto", "serper", "sk-1", set_by="op")

    out = f.remettre_au_format(dry_run=False)

    assert _verdicts(out) == {("transcription", "oto"): f.AU_FORMAT}, \
        "un mono-champ n'est pas concerné : sa valeur brute EST son format"
    assert _ligne("oto", "serper")["clair"] == "sk-1"


def test_une_ligne_illisible_leve_et_rien_n_est_reecrit(base):
    from oto_mcp import credentials_store, crypto
    from oto_mcp import cles_plateforme_format as f
    from oto_mcp.db import _connect
    _poser_brute("a-oto", "forager", "fg-brute")
    _poser_brute("b-illisible", "forager", "fg-autre")
    with _connect() as conn:
        conn.execute(
            "UPDATE connector_credentials SET secret_enc = %s WHERE entity_type = %s "
            "AND entity_id = 'b-illisible'",
            (crypto.encrypt("x", "une autre ligne"), credentials_store.PLATFORM))
    avant = _ligne("a-oto", "forager")

    with pytest.raises(f.CleIllisible, match="b-illisible") as e:
        f.remettre_au_format(dry_run=False)

    assert "fg-" not in str(e.value)
    assert _ligne("a-oto", "forager") == avant, "rien réécrit, même la ligne lisible"


def test_la_commande_est_a_blanc_par_defaut_et_ecrit_sous_apply(base):
    from oto_mcp import credentials_store, maintenance
    _poser_brute("oto", "forager", "fg-brute")

    assert maintenance.main(["cles-plateforme-au-format", "--strict"]) == 0
    assert _ligne("oto", "forager")["clair"] == "fg-brute"

    assert maintenance.main(["cles-plateforme-au-format", "--apply", "--strict"]) == 0
    assert credentials_store.unpack_secret(
        "forager", _ligne("oto", "forager")["clair"]) == {"api_key": "fg-brute"}
