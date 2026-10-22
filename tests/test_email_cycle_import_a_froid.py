"""Le trio `email` / `email_brand` / `email_templates` s'importe par N'IMPORTE lequel.

Le 22/10/2026, la maintenance quotidienne a rendu « alertes-credential : ÉCHEC après
7 ms — ImportError: cannot import name 'send_invite_email' from partially initialized
module 'oto_mcp.email_templates' ». Le timer n'importe aucun module d'email avant ce
travail : `alertes_credential` entre dans le trio PAR `email_templates`, qui importe
`email`, dont le corps faisait `from .email_templates import send_invite_email` — un
nom que `email_templates`, à moitié chargé, n'avait pas encore défini. Les bancs du
travail ne le voyaient pas : dans pytest, un autre test a toujours chargé `email`
avant.

D'où un interpréteur NEUF par cas : l'ordre d'entrée est tout le défaut.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

RACINE = pathlib.Path(__file__).resolve().parent.parent


def _python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=RACINE,
                          capture_output=True, text=True, timeout=120)


def test_la_maintenance_joue_alertes_credential_a_froid():
    """L'ordre exact du timer : `maintenance`, puis le travail — sans base (rien à
    prévenir), mais le corps du travail court jusqu'au bout, import compris."""
    r = _python("""
        import sys
        from oto_mcp import maintenance as M
        from oto_mcp.db import alertes_credential as A
        from oto_mcp.db import runner_triggers
        assert "oto_mcp.email" not in sys.modules, "plus à froid : le banc ne prouve rien"
        A.a_notifier = lambda: []
        A.clore_episodes_gueris = lambda: 0
        A.cles_ko_a_annoncer = lambda: []
        out = M.alertes_credential(dry_run=True)
        assert out["orgs_a_prevenir"] == 0, out
        from oto_mcp import email_templates
        assert callable(email_templates.send_credential_alert_email)
        print("OK")
    """)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stderr[-2000:]


@pytest.mark.parametrize("entree", ["email_templates", "email", "email_brand"])
def test_chaque_entree_du_trio_rend_les_gabarits_reexposes(entree):
    """Quelle que soit la porte, `email.send_*` (ce que les appelants et les tests
    lisent) est le gabarit d'`email_templates`, et un nom inconnu lève toujours."""
    r = _python(f"""
        import importlib
        importlib.import_module("oto_mcp.{entree}")
        from oto_mcp import email, email_templates
        for nom in ("send_invite_email", "send_resource_shared_email",
                    "send_resource_transferred_email", "send_signal_digest_email",
                    "send_process_readers_digest_email"):
            assert getattr(email, nom) is getattr(email_templates, nom), nom
        try:
            email.send_inexistant
        except AttributeError:
            print("OK")
    """)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stderr[-2000:]
