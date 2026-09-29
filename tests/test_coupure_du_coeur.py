"""La coupure du cœur (#1097) : le cœur ne pose plus aucun droit, oto-commerce les pose.

Depuis la bascule, le service de facturation (oto-commerce) pose SEUL les droits déclarés
(`org_entitlements`) par l'API de service. Ce fichier tient les trois faces de la coupure :

1. **la réconciliation interne ne revient pas** — ni son module, ni un appel, ni son
   travail de maintenance ;
2. **les gestes qui vendaient, offraient ou déclaraient un droit refusent**, nommément,
   en 409 `billing_moved`, avant tout appel au domaine — et le refus est DÉCLARÉ ;
3. les options hors catalogue restent posables (`test_option_compose.py`), la garde de
   `has_option` (`test_billing_b4_entitlement.py`) et celle du runner
   (`test_billing_b3_runner.py`) vivent avec leurs voisines.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from oto_mcp import billing, maintenance
from oto_mcp.capabilities import billing as cap_billing
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

PKG = pathlib.Path(billing.__file__).parent
_NOMS_INTERDITS = {"reconcilier", "reconcilier_tout", "reconcilier_droits",
                   "billing_droits", "droits_voulus", "_reposer_droits"}


# ── 1. la réconciliation interne ne revient pas ──────────────────────────────

def _noms(arbre: ast.AST) -> set[str]:
    vus: set[str] = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Name):
            vus.add(n.id)
        elif isinstance(n, ast.Attribute):
            vus.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            vus.add(n.name)
        elif isinstance(n, ast.Import):
            vus |= {a.name.split(".")[-1] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            vus.add((n.module or "").split(".")[-1])
            vus |= {a.name for a in n.names}
    return vus


def test_aucun_module_ne_reconcilie_les_droits():
    fautifs = {}
    for f in sorted(PKG.rglob("*.py")):
        lus = _noms(ast.parse(f.read_text(encoding="utf-8"))) & _NOMS_INTERDITS
        if lus:
            fautifs[str(f.relative_to(PKG))] = sorted(lus)
    assert not fautifs, (
        f"la réconciliation interne des droits revient : {fautifs}. Depuis la coupure "
        "du cœur (#1097), oto-commerce pose seul les droits déclarés, par l'API de "
        "service ; le cœur n'en écrit aucun.")
    assert not (PKG / "billing_droits.py").exists()


def test_le_garde_voit_un_import_tardif_et_un_appel():
    src = ("def f():\n    from . import billing_droits\n"
           "    billing_droits.reconcilier_tout()\n")
    assert {"billing_droits", "reconcilier_tout"} <= _noms(ast.parse(src))


# ── 1 bis. l'AXE : un seul écrivain des droits déclarés ──────────────────────
# Les noms bannis ci-dessus ne gardent que la FORME de l'ancien producteur. L'axe, c'est
# « qui écrit `org_entitlements` » : la pose et le retrait (`db/entitlements.grant` /
# `revoke`) ne s'appellent que depuis l'API de service d'oto-commerce, quel que soit le
# nom sous lequel le module est importé.

_ECRIVAIN_SEUL = "capabilities/service_commerce.py"
_MODULE_DES_DROITS = "oto_mcp.db.entitlements"
_ECRITURES = {"grant", "revoke"}


def _module_absolu(fichier: pathlib.Path, noeud: ast.ImportFrom) -> str:
    """Le module qu'un `from … import` désigne, relatif résolu depuis `fichier`."""
    if not noeud.level:
        return noeud.module or ""
    paquet = list(fichier.relative_to(PKG.parent).with_suffix("").parts[:-1])
    base = paquet[:len(paquet) - (noeud.level - 1)] if noeud.level > 1 else paquet
    return ".".join(base + ([noeud.module] if noeud.module else []))


def _ecritures_des_droits(fichier: pathlib.Path, arbre: ast.AST) -> list[str]:
    """Les appels à `grant` / `revoke` du module des droits dans ce fichier, quel que
    soit l'alias : `from ..db import entitlements as X` → `X.grant(…)` ;
    `from ..db.entitlements import grant as g` → `g(…)` ; `import
    oto_mcp.db.entitlements as X` ; et la forme longue `db.entitlements.grant(…)`."""
    alias_module: set[str] = set()
    alias_fonction: set[str] = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.ImportFrom):
            mod = _module_absolu(fichier, n)
            for a in n.names:
                if mod == _MODULE_DES_DROITS and a.name in _ECRITURES:
                    alias_fonction.add(a.asname or a.name)
                elif f"{mod}.{a.name}" == _MODULE_DES_DROITS:
                    alias_module.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == _MODULE_DES_DROITS and a.asname:
                    alias_module.add(a.asname)
    vus = []
    for n in ast.walk(arbre):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in alias_fonction:
            vus.append(f"{f.id}() l.{n.lineno}")
        elif isinstance(f, ast.Attribute) and f.attr in _ECRITURES:
            porteur = f.value
            if isinstance(porteur, ast.Name) and porteur.id in alias_module:
                vus.append(f"{porteur.id}.{f.attr}() l.{n.lineno}")
            elif (isinstance(porteur, ast.Attribute) and porteur.attr == "entitlements"
                  and isinstance(porteur.value, ast.Name) and porteur.value.id == "db"):
                vus.append(f"db.entitlements.{f.attr}() l.{n.lineno}")
            elif (isinstance(porteur, ast.Attribute)
                  and ast.unparse(porteur) == _MODULE_DES_DROITS):
                vus.append(f"{_MODULE_DES_DROITS}.{f.attr}() l.{n.lineno}")
    return vus


def test_seule_l_api_du_commerce_ecrit_les_droits_declares():
    ecrivains = {}
    for f in sorted(PKG.rglob("*.py")):
        rel = str(f.relative_to(PKG))
        if rel == "db/entitlements.py":
            continue
        vus = _ecritures_des_droits(f, ast.parse(f.read_text(encoding="utf-8")))
        if vus:
            ecrivains[rel] = vus
    assert set(ecrivains) == {_ECRIVAIN_SEUL}, (
        f"écrivains de `org_entitlements` : {ecrivains}. Depuis la coupure du cœur "
        "(#1097), seule l'API de service d'oto-commerce pose ou retire un droit déclaré.")


@pytest.mark.parametrize("src", [
    "from ..db import entitlements as E\ndef f():\n    E.grant(1, 'unipile', 'trial', value=1)\n",
    "from ..db.entitlements import revoke as r\ndef f():\n    r(1, 'unipile', 'trial')\n",
    "import oto_mcp.db.entitlements as droits\ndef f():\n    droits.grant(1, 'x', 'y', value=1)\n",
    "from .. import db\ndef f():\n    db.entitlements.revoke(1, 'x', 'y')\n",
    "def f():\n    from ..db import entitlements\n    entitlements.grant(1, 'x', 'y', value=1)\n",
])
def test_le_garde_de_l_axe_voit_chaque_alias(src):
    fichier = PKG / "capabilities" / "exemple.py"
    assert _ecritures_des_droits(fichier, ast.parse(src)), src


def test_le_garde_de_l_axe_ne_confond_pas_un_autre_grant():
    fichier = PKG / "capabilities" / "exemple.py"
    src = ("from .. import ownership, credentials_store\n"
           "def f():\n    ownership.grant('t', 'r', 'user', 'u')\n"
           "    credentials_store.platform_grant('p', 'org:1')\n")
    assert _ecritures_des_droits(fichier, ast.parse(src)) == []


def test_plus_aucun_travail_de_maintenance_ne_realigne_les_droits():
    assert "droits" not in maintenance._TRAVAUX
    assert "droits" not in maintenance._ALL


def test_la_resiliation_et_la_reprise_n_ont_plus_de_domaine():
    """Refusées à la capacité, elles n'ont plus rien à faire au domaine : un appel
    qui les contournerait n'aurait rien à exécuter."""
    assert not hasattr(billing, "cancel") and not hasattr(billing, "resume")


# ── 2. les gestes refusent, avant le domaine ─────────────────────────────────

CTX = ResolvedCtx(sub="u-admin", org_id=7, role="admin")

_GESTES = [
    ("billing.subscribe", cap_billing._subscribe,
     cap_billing.SubscribeInput(plan="standard", return_url="https://exemple.test/b")),
    ("billing.confirm", cap_billing._confirm, cap_billing.ConfirmInput()),
    ("billing.method_change", cap_billing._method_change_start,
     cap_billing.MethodChangeInput(return_url="https://exemple.test/b")),
    ("billing.method_change_confirm", cap_billing._method_change_confirm,
     cap_billing.ConfirmInput()),
    ("billing.admin_set_plan", cap_billing._admin_set_plan,
     cap_billing.AdminPlanInput(org_id=7, plan="business")),
    ("billing.admin_set_plan", cap_billing._admin_set_plan,
     cap_billing.AdminPlanInput(org_id=7, plan=None)),
    ("billing.admin_set_contract", cap_billing._admin_set_contract,
     cap_billing.AdminContractInput(org_id=7, plan="standard", seats=3)),
    ("billing.admin_cancel_contract", cap_billing._admin_cancel_contract,
     cap_billing.AdminContractCancelInput(org_id=7)),
    ("billing.cancel", cap_billing._cancel, cap_billing.NoInput()),
    ("billing.resume", cap_billing._resume, cap_billing.NoInput()),
]


@pytest.fixture
def domaine_interdit(monkeypatch):
    """Aucun geste refusé ne touche le domaine, ni le prestataire de paiement."""
    from oto_mcp import billing_method, mollie_client

    def _jamais(*a, **k):
        raise AssertionError("le refus doit tomber AVANT le domaine")

    for nom in ("subscribe", "confirm", "status"):
        monkeypatch.setattr(billing, nom, _jamais)
    for nom in ("mark_cancel_at_period_end", "resume_canceled",
                "upsert_org_subscription"):
        monkeypatch.setattr(billing.db_billing, nom, _jamais)
    for nom in ("start", "confirm"):
        monkeypatch.setattr(billing_method, nom, _jamais)
    for nom in ("create_first_payment", "get_payment", "create_recurring_payment"):
        if hasattr(mollie_client, nom):
            monkeypatch.setattr(mollie_client, nom, _jamais)


@pytest.mark.parametrize("cle,handler,inp", _GESTES,
                         ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(_GESTES)])
def test_chaque_geste_de_facturation_refuse_billing_moved(domaine_interdit, cle,
                                                          handler, inp):
    with pytest.raises(AuthzDenied) as e:
        handler(CTX, inp)
    assert (e.value.status, e.value.code) == (409, "billing_moved")
    assert "oto-commerce" in e.value.message


_REFUSANTES = sorted({c for c, _, _ in _GESTES}
                     | {"platform.option.set", "platform.connector.access_set"})


@pytest.mark.parametrize("cle", _REFUSANTES)
def test_le_refus_est_declare(cle):
    cap = next(c for c in CAPABILITIES if c.key == cle)
    assert (409, "billing_moved") in {(e.status, e.code) for e in cap.errors}
    assert "billing_moved" in (cap.description or "")


@pytest.mark.parametrize("cle", ["billing.status", "billing.plans", "billing.payments"])
def test_les_lectures_restent_servies(cle):
    cap = next(c for c in CAPABILITIES if c.key == cle)
    assert "billing_moved" not in {e.code for e in cap.errors}


def test_la_route_rend_le_refus_dans_l_enveloppe(monkeypatch):
    from _datastore_rest import call, stub_authz

    stub_authz(monkeypatch, org_id=7, role="admin")
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: True)
    code, corps = call("billing.admin_set_contract", path_params={"org_id": 7},
                       body={"plan": "standard", "seats": 3})
    assert (code, corps["error"]) == (409, "billing_moved"), corps
    assert "oto-commerce" in corps["detail"]
