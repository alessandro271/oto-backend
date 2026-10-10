"""Le document d'ADMINISTRATION et son contrat (#1196).

Les opérations `/api/admin/*` n'apparaissaient dans aucun document : un consommateur qui
les appelle (un front d'instance, un service d'usage) ne pouvait ni les épingler, ni être
prévenu quand l'une d'elles casse. D'où :

- `openapi.build(admin=True)` — toutes les opérations `/api/admin/*`, elles seules, sans
  liste à tenir ; le document public, lui, ne change pas d'un octet ;
- `GET /api/admin/openapi.json` (capacité `admin.openapi`), derrière la garde d'admin
  plateforme ;
- un second contrat par consommateur (`chemin_admin`), jugé AVANT le déploiement
  seulement (`contrat-front-code`, document dérivé par import, sans jeton — ce câblage
  est tenu par `test_workflow_contrat_admin_1196.py`).

Ce banc tient le document (complet contre la table figée, disjoint du public, servi ==
dérivé, `operationId` uniques et stables pour ce qu'un consommateur épingle), la garde, et
prouve que le contrôle MORD sur le document réel : une opération épinglée retirée, une
entrée devenue obligatoire, rougissent en le nommant.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from oto_mcp import openapi
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._authz import PLATFORM_ADMIN
from oto_mcp.capabilities._types import ResolvedCtx

_RACINE = pathlib.Path(__file__).resolve().parents[1]
_JUGE = _RACINE / "scripts" / "contrat-front.py"
_TABLE = _RACINE / "tests" / "api" / "api_routes_table.txt"
_ADMIN = "/api/admin/"


class _FakeRoute:
    def __init__(self, path, methods):
        self.path = path
        self.methods = set(methods)


def _operations(doc: dict) -> set[tuple[str, str]]:
    return {(m, p) for p, ops in doc["paths"].items() for m in ops}


def _bindings_exposes() -> set[tuple[str, str]]:
    return {(b.verb.lower(), openapi._openapi_path(b.path))
            for cap in registry.CAPABILITIES if cap.is_exposed()
            for b in cap.rest_bindings()}


def _routes_vivantes():
    from oto_mcp import server
    return server.routes_rest(object())


def _admin_openapi():
    return next(c for c in registry.CAPABILITIES if c.key == "admin.openapi")


def _console_de_la_table() -> set[tuple[str, str]]:
    """(verbe, chemin OpenAPI) de chaque opération `/api/admin/*` de la table figée,
    `OPTIONS` et `HEAD` exclus (préflight et miroir de GET, jamais décrits)."""
    ops = set()
    for ligne in _TABLE.read_text(encoding="utf-8").splitlines():
        verbes, chemin, _ = ligne.split(" ", 2)
        if not chemin.startswith(_ADMIN):
            continue
        for verbe in verbes.split(","):
            if verbe not in ("OPTIONS", "HEAD"):
                ops.add((verbe.lower(), openapi._openapi_path(chemin)))
    return ops


# ── Le document ───────────────────────────────────────────────────────────────

def test_le_document_admin_decrit_toute_la_console_de_la_table_figee(monkeypatch):
    """Complet contre la table figée des routes : pas « quelques-unes », pas « celles
    d'un consommateur » (décision du 09/10/2026)."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    doc = openapi.build(_routes_vivantes(), admin=True)
    attendu = _console_de_la_table()
    assert len(attendu) >= 90, "la table figée a perdu sa console ?"
    manquantes = {(v, p) for v, p in attendu
                  if p not in doc["paths"] or v not in doc["paths"][p]}
    assert not manquantes, f"opérations admin absentes du document admin : {sorted(manquantes)}"


def test_le_document_admin_porte_toutes_les_operations_admin_et_elles_seules(monkeypatch):
    """Complet sans liste : chaque binding `/api/admin/*` d'une capacité exposée y est, et
    rien d'autre n'y entre."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    doc = openapi.build(admin=True)
    attendues = {(m, p) for m, p in _bindings_exposes() if p.startswith(_ADMIN)}
    assert attendues and attendues <= _operations(doc)
    assert all(p.startswith(_ADMIN) for p in doc["paths"]), "une route publique y est entrée"


def test_le_document_public_ne_porte_aucune_operation_admin_drapeau_omis_ou_faux():
    routes = [*_routes_vivantes(), _FakeRoute("/api/admin/platform-keys", ["GET"])]
    for doc in (openapi.build(routes), openapi.build(routes, admin=False)):
        assert not [p for p in doc["paths"] if p.startswith(_ADMIN)]


def test_le_document_public_est_le_meme_avec_ou_sans_drapeau(monkeypatch):
    """Séparer l'administration n'a pas touché le document public : `build()` et
    `build(admin=False)` rendent le même texte, à l'octet."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    routes = _routes_vivantes()
    sans = json.dumps(openapi.build(routes), sort_keys=True)
    faux = json.dumps(openapi.build(routes, admin=False), sort_keys=True)
    assert sans == faux


def test_les_deux_documents_se_partagent_le_prefixe_jamais_un_chemin(monkeypatch):
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    routes = _routes_vivantes()
    public, admin = openapi.build(routes), openapi.build(routes, admin=True)
    assert not [p for p in public["paths"] if p.startswith(_ADMIN)]
    assert not set(public["paths"]) & set(admin["paths"])
    # Rien de ce que sert une capacité ne tombe entre les deux.
    assert _bindings_exposes() <= _operations(public) | _operations(admin)


def test_le_document_admin_se_nomme_et_le_public_garde_son_titre():
    routes = _routes_vivantes()
    assert openapi.build(routes, admin=True)["info"]["title"] == openapi._TITLE_ADMIN
    assert openapi.build(routes)["info"]["title"] == openapi._TITLE
    assert openapi._TITLE_ADMIN != openapi._TITLE


def test_le_document_servi_egale_celui_que_la_ci_derive(monkeypatch):
    """Le handler ne voit pas la table de routes ; la CI dérive de `server.routes_rest`.
    Égaux tant qu'aucune route d'admin n'est écrite à la main — si l'une l'était, elle
    manquerait au document servi, et ce banc rougit avant."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    cap = _admin_openapi()
    servi = cap.handler(ResolvedCtx(sub="admin-de-test"), cap.Input())
    assert servi == openapi.build(_routes_vivantes(), admin=True)


def test_la_route_est_derriere_la_garde_d_admin_plateforme():
    cap = _admin_openapi()
    assert cap.authz is PLATFORM_ADMIN
    assert [(b.verb, b.path) for b in cap.rest_bindings()] == [
        ("GET", "/api/admin/openapi.json")]
    assert cap.mcp is None, "REST seule, comme /api/admin/capabilities"


def test_chaque_operationId_est_unique_dans_le_document_admin(monkeypatch):
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    doc = openapi.build(_routes_vivantes(), admin=True)
    ids = [op["operationId"] for item in doc["paths"].values()
           for verbe, op in item.items() if verbe in ("get", "post", "put", "patch", "delete")]
    doublons = sorted({i for i in ids if ids.count(i) > 1})
    assert not doublons, f"operationId en double dans le document admin : {doublons}"


def test_une_capacite_admin_porte_son_id_de_capacite_meme_quand_le_verbe_a_un_jumeau_public(monkeypatch):
    """L'`operationId` qu'un consommateur de la console épingle est `{clé}_{verbe}`, pour
    une capacité purement admin COMME pour une capacité à deux bindings du même verbe (un
    public, un admin : membres d'org, secrets d'org). Le document admin ne porte que le
    binding admin, donc il est le premier de son verbe et garde l'id de la capacité —
    c'est ce que le dashboard a épinglé ; un id dérivé du chemin casserait son contrat."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    doc = openapi.build(admin=True)
    pures, mixtes = 0, 0
    for cap in registry.CAPABILITIES:
        if not cap.is_exposed():
            continue
        liaisons = cap.rest_bindings()
        admins = [b for b in liaisons if b.path.startswith(_ADMIN)]
        if not admins:
            continue
        for b in admins:
            if [x for x in admins if x.verb == b.verb].index(b) != 0:
                continue      # deux bindings admin du même verbe : seul le premier porte l'id nu
            op = doc["paths"][openapi._openapi_path(b.path)][b.verb.lower()]
            assert op["operationId"] == f"{cap.key}.{b.verb.lower()}".replace(".", "_"), \
                (cap.key, b.verb, b.path, op["operationId"])
        if len(admins) == len(liaisons):
            pures += 1
        elif any(p.verb == a.verb for p in liaisons for a in admins if p not in admins):
            mixtes += 1
    assert pures, "aucune capacité purement admin dans le registre ?"
    assert mixtes, "aucune capacité à binding public ET admin du même verbe ?"


# ── Le contrat ────────────────────────────────────────────────────────────────

def _epingler(doc: dict, operations: list[tuple[str, str]]) -> dict:
    paths: dict = {}
    for m, p in operations:
        paths.setdefault(p, {})[m] = json.loads(json.dumps(doc["paths"][p][m]))
    return {"openapi": doc["openapi"], "paths": paths, "components": doc["components"]}


def _juger(tmp_path, epingle: dict, derive: dict):
    e, d = tmp_path / "epingle.json", tmp_path / "derive.json"
    e.write_text(json.dumps(epingle))
    d.write_text(json.dumps(derive))
    return subprocess.run([sys.executable, str(_JUGE), "--consommateur",
                           "oto-dashboard (admin)", str(e), str(d)],
                          capture_output=True, text=True)


_EPINGLEES = [("get", "/api/admin/users"), ("post", "/api/admin/orgs/{id}/members"),
              ("get", "/api/admin/monitoring/summary")]


def test_le_controle_juge_le_contrat_admin_et_mord(tmp_path, monkeypatch):
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    derive = openapi.build(admin=True)
    epingle = _epingler(derive, _EPINGLEES)

    vert = _juger(tmp_path, epingle, derive)
    assert vert.returncode == 0, vert.stdout + vert.stderr
    assert "3 opération(s) épinglée(s) par « oto-dashboard (admin) »" in vert.stdout

    # 1. Une opération épinglée retirée du document.
    sans = json.loads(json.dumps(derive))
    del sans["paths"]["/api/admin/users"]["get"]
    rouge = _juger(tmp_path, epingle, sans)
    assert rouge.returncode == 1
    assert "OPÉRATIONS DISPARUES" in rouge.stdout and "GET /api/admin/users" in rouge.stdout
    assert "CASSENT les appels de « oto-dashboard (admin) »" in rouge.stdout

    # 2. Une entrée devenue obligatoire.
    durci = json.loads(json.dumps(derive))
    corps = (durci["paths"]["/api/admin/orgs/{id}/members"]["post"]["requestBody"]
             ["content"]["application/json"]["schema"])
    libre = sorted(set(corps["properties"]) - set(corps.get("required") or []))
    assert libre, "il faut un champ facultatif pour éprouver le durcissement"
    corps["required"] = sorted({*(corps.get("required") or []), libre[0]})
    rouge = _juger(tmp_path, epingle, durci)
    assert rouge.returncode == 1
    assert f"le champ « {libre[0]} » du corps de requête devient obligatoire" in rouge.stdout
