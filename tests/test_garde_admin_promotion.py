"""Nul n'est NOMMÉ admin plateforme sans second facteur fort — `garde_admin.refus_promotion`.

Ce que ce banc fige, sur chaque chemin qui écrit `users.role` vers `admin`/`super_admin` :

1. la règle (`refus_promotion`) : `off` ne lit rien ; `enforce` refuse un compte sans
   facteur fort (`BackupCode` seul ne compte pas) et un compte dont les facteurs sont
   illisibles (fail-closed) ; `report` journalise et laisse passer ; rétrograder ne lit
   jamais Logto ;
2. la capacité `platform.user.set_role` — route REST `POST /api/admin/users/{sub}/role`
   et `oto_admin_user op=set_role` partagent son handler : 409
   `admin_mfa_required_for_promotion` (403 `admin_mfa_unverifiable` quand les facteurs
   sont illisibles), rien n'est écrit ;
3. le script de maintenance `scripts/migrate_admin_to_super.py` : en `enforce`, un seul
   compte sans facteur et rien n'est écrit.
"""
from __future__ import annotations

import logging
import pathlib
import sys

import pytest

from oto_mcp import garde_admin
from oto_mcp.capabilities import admin_console, registry, users_admin
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

# La racine du dépôt n'est pas dans `sys.path` en CI (pas de `__init__.py`) : `scripts`
# ne s'importe qu'ainsi (même geste que `tests/test_deplacer_cles_org.py`).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CIBLE = "u-cible"
CTX = ResolvedCtx(sub="super-1")


@pytest.fixture
def facteurs(monkeypatch):
    """Les facteurs Logto, doublés et COMPTÉS (le défaut du conftest est remplacé)."""
    etat = {"types": ["Totp"], "appels": 0, "leve": None}

    def lire(sub_logto):
        etat["appels"] += 1
        if etat["leve"]:
            raise etat["leve"]
        return list(etat["types"])
    monkeypatch.setattr(garde_admin, "_facteurs_logto", lire)
    garde_admin.oublier()
    return etat


@pytest.fixture
def mode(monkeypatch):
    def poser(valeur):
        if valeur is None:
            monkeypatch.delenv("OTO_ADMIN_MFA", raising=False)
        else:
            monkeypatch.setenv("OTO_ADMIN_MFA", valeur)
    return poser


@pytest.fixture
def base(monkeypatch):
    """Le compte cible existe ; chaque écriture de rôle est relevée."""
    ecrits = []
    monkeypatch.setattr(users_admin, "_resolve_target", lambda t: t)
    monkeypatch.setattr(users_admin.db, "get_user", lambda sub: {"sub": sub, "role": "member"})
    monkeypatch.setattr(users_admin.db, "set_user_role",
                        lambda sub, role: ecrits.append((sub, role)))
    return ecrits


# ── 1. La règle ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valeur", [None, "", "off"])
def test_off_ne_lit_rien_et_laisse_nommer(facteurs, mode, valeur):
    mode(valeur)
    facteurs["types"] = []
    assert garde_admin.refus_promotion(CIBLE, "super_admin") is None
    assert facteurs["appels"] == 0


@pytest.mark.parametrize("types_", [["Totp"], ["WebAuthn"], ["Totp", "BackupCode"]])
@pytest.mark.parametrize("valeur", ["enforce", "report"])
def test_un_facteur_fort_laisse_nommer(facteurs, mode, types_, valeur):
    mode(valeur)
    facteurs["types"] = types_
    assert garde_admin.refus_promotion(CIBLE, "admin") is None
    assert facteurs["appels"] == 1


@pytest.mark.parametrize("types_", [[], ["BackupCode"], ["Sms"]])
@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_enforce_refuse_sans_facteur_fort(facteurs, mode, types_, role):
    mode("enforce")
    facteurs["types"] = types_
    m = garde_admin.refus_promotion(CIBLE, role)
    assert m is not None and m.code == garde_admin.CODE_PROMOTION
    assert "enrols" in m.message


def test_une_valeur_inconnue_vaut_enforce(facteurs, mode):
    mode("enforse")
    facteurs["types"] = []
    assert garde_admin.refus_promotion(CIBLE, "admin").code == garde_admin.CODE_PROMOTION


def test_enforce_logto_injoignable_refuse(facteurs, mode):
    mode("enforce")
    facteurs["leve"] = TimeoutError("logto")
    m = garde_admin.refus_promotion(CIBLE, "admin")
    assert m is not None and m.code == garde_admin.CODE_INVERIFIABLE
    assert "could not be verified" in m.message


@pytest.mark.parametrize("panne", [False, True])
def test_report_journalise_et_laisse_nommer(facteurs, mode, caplog, panne):
    mode("report")
    facteurs["types"] = []
    if panne:
        facteurs["leve"] = TimeoutError("logto")
    with caplog.at_level(logging.WARNING, logger="oto_mcp.garde_admin"):
        assert garde_admin.refus_promotion(CIBLE, "super_admin") is None
    attendu = garde_admin.CODE_INVERIFIABLE if panne else garde_admin.CODE_MFA
    assert any("garde admin (report)" in r.getMessage() and CIBLE in r.getMessage()
               and attendu in r.getMessage() for r in caplog.records)


def test_retrograder_ne_lit_jamais_logto(facteurs, mode):
    mode("enforce")
    facteurs["types"] = []
    assert garde_admin.refus_promotion(CIBLE, "member") is None
    assert facteurs["appels"] == 0


def test_la_lecture_est_fraiche_pas_le_cache(facteurs, mode):
    """Un verdict positif en cache (5 min) ne nomme pas un compte qui a retiré son
    facteur depuis : la nomination relit Logto."""
    mode("enforce")
    garde_admin._lire_et_cacher(CIBLE)          # en cache : Totp
    facteurs["types"] = []
    assert garde_admin.refus_promotion(CIBLE, "admin").code == garde_admin.CODE_PROMOTION
    assert facteurs["appels"] == 2


# ── 2. La capacité : route REST et `oto_admin_user op=set_role` ──────────────

def test_route_rest_et_outil_mcp_passent_par_le_meme_handler():
    caps = {c.key: c for c in registry.CAPABILITIES}
    cap = caps["platform.user.set_role"]
    assert cap.handler is users_admin._set_role
    assert (cap.rest.verb, cap.rest.path) == ("POST", "/api/admin/users/{sub}/role")
    assert caps["admin.user"].mcp == "oto_admin_user"


def _par_rest(role):
    return users_admin._set_role(CTX, users_admin.SetRoleInput(target=CIBLE, role=role))


def _par_mcp(role):
    return admin_console._user(CTX, admin_console.UserAdminInput(
        op="set_role", target=CIBLE, role=role))


CHEMINS = pytest.mark.parametrize("chemin", [_par_rest, _par_mcp], ids=["rest", "mcp"])


@CHEMINS
def test_nomination_refusee_sans_facteur(base, facteurs, mode, chemin):
    mode("enforce")
    facteurs["types"] = ["BackupCode"]
    with pytest.raises(AuthzDenied) as e:
        chemin("admin")
    assert (e.value.status, e.value.code) == (409, garde_admin.CODE_PROMOTION)
    assert base == [], "rien n'est écrit"


@CHEMINS
def test_nomination_refusee_logto_injoignable(base, facteurs, mode, chemin):
    mode("enforce")
    facteurs["leve"] = ConnectionError("logto")
    with pytest.raises(AuthzDenied) as e:
        chemin("super_admin")
    assert (e.value.status, e.value.code) == (403, garde_admin.CODE_INVERIFIABLE)
    assert base == []


@CHEMINS
def test_nomination_acceptee_avec_facteur(base, facteurs, mode, chemin):
    mode("enforce")
    facteurs["types"] = ["WebAuthn"]
    assert chemin("super_admin")["role"] == "super_admin"
    assert base == [(CIBLE, "super_admin")]


@CHEMINS
@pytest.mark.parametrize("valeur", [None, "off", "report"])
def test_nomination_passe_en_off_et_report(base, facteurs, mode, chemin, valeur):
    mode(valeur)
    facteurs["types"] = []
    assert chemin("admin")["role"] == "admin"
    assert base == [(CIBLE, "admin")]


@CHEMINS
def test_retrograder_passe_toujours(base, facteurs, mode, chemin):
    mode("enforce")
    facteurs["types"] = []
    assert chemin("member")["role"] == "member"
    assert base == [(CIBLE, "member")] and facteurs["appels"] == 0


# ── 3. Le script de maintenance ──────────────────────────────────────────────

class _Curseur:
    def __init__(self, lignes, ecrits):
        self.lignes, self.ecrits, self.rowcount = lignes, ecrits, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        if sql.lstrip().startswith("UPDATE"):
            self.ecrits.append(sql)
            self.rowcount = len(self.lignes)

    def fetchall(self):
        return list(self.lignes)


class _Connexion:
    def __init__(self, lignes, ecrits):
        self.lignes, self.ecrits = lignes, ecrits

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Curseur(self.lignes, self.ecrits)

    def commit(self):
        self.ecrits.append("commit")


@pytest.fixture
def script(monkeypatch):
    from scripts import migrate_admin_to_super as m
    ecrits = []
    lignes = [("a-1", "a1@example.test"), ("a-2", None)]
    monkeypatch.setenv("DATABASE_URL", "postgresql://banc/factice")
    monkeypatch.setattr(m.psycopg, "connect", lambda dsn: _Connexion(lignes, ecrits))
    monkeypatch.setattr(m.sys, "argv", ["migrate_admin_to_super"])
    return m, ecrits


def test_script_enforce_un_compte_sans_facteur_rien_n_est_ecrit(script, monkeypatch, mode):
    m, ecrits = script
    mode("enforce")
    garde_admin.oublier()
    monkeypatch.setattr(garde_admin, "_facteurs_logto",
                        lambda sub: ["Totp"] if sub == "a-1" else [])
    with pytest.raises(SystemExit) as e:
        m.main()
    assert "1 compte(s)" in str(e.value.code)
    assert ecrits == []


def test_script_enforce_tous_equipes_ecrit(script, facteurs, mode):
    m, ecrits = script
    mode("enforce")
    m.main()
    assert ecrits[-1] == "commit" and any("super_admin" in e for e in ecrits[:-1])


@pytest.mark.parametrize("valeur", [None, "report"])
def test_script_off_et_report_ecrivent(script, facteurs, mode, valeur):
    m, ecrits = script
    mode(valeur)
    facteurs["types"] = []
    m.main()
    assert ecrits[-1] == "commit"
