"""La release annoncée à Sentry : `OTO_SENTRY_RELEASE` s'il est posé, sinon
l'étiquette de la version servie — jamais `unknown`."""
from __future__ import annotations

from oto_mcp import sentry_setup, version


def test_la_variable_posee_gagne(monkeypatch):
    monkeypatch.setenv("OTO_SENTRY_RELEASE", "rel-explicite")
    monkeypatch.setenv(version.ENV_REF, "v1.2.3")
    assert sentry_setup._release() == "rel-explicite"


def test_sans_variable_la_release_est_la_version_servie(monkeypatch):
    monkeypatch.delenv("OTO_SENTRY_RELEASE", raising=False)
    monkeypatch.setenv(version.ENV_REF, "v1.2.3")
    monkeypatch.setenv(version.ENV_SHA, "6d5bf16b0123456789")
    assert sentry_setup._release() == "v1.2.3+6d5bf16b"


def test_version_inconnue_pas_de_release(monkeypatch):
    monkeypatch.delenv("OTO_SENTRY_RELEASE", raising=False)
    monkeypatch.setattr(version, "_coordonnees",
                        lambda: (None, None, None, version.INCONNU))
    assert sentry_setup._release() is None
