"""`pennylane_firm_upload_urls` — la frappe PAR LOTS, son manifeste, sa route, son script.

Sans base, comme la CI : le client de la lib est remplacé sur son paquet, l'org de
l'appel, la garde d'identité et l'appartenance sont posées par `monkeypatch`. Ce que
ces épreuves tiennent :

- un lot de 1 à 100 éléments ; chaque élément est contrôlé seul (nom, doublon dans le
  lot, dossier introuvable, nom déjà présent, dossier trop grand) sans faire échouer
  les autres ; une erreur de Pennylane qui n'est pas propre à un élément fait échouer
  TOUT l'appel, sans un seul lien frappé ;
- une lecture par dossier DISTINCT (existence, puis noms) ;
- la réponse ne porte AUCUNE URL de lien : elles vivent dans le manifeste, borné,
  expirant, servi en TSV par `GET /api/relay/batch/{token}` sous un `typ` à lui ;
- le script fourni joint `paths.tsv` au manifeste, envoie sur 4 flux au plus, et
  signale `NO_LINK` sans envoyer — joué pour de vrai par `bash`, `curl` doublé.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp.mcp_errors import McpError

ORG = 7
SUB = "u-cabinet"
BASE = "https://oto.example.test"
DOSSIERS = {5, 6}


def _outil():
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane_firm as P

    m = FastMCP("t")
    P.register(m)
    return {t.name: t for t in asyncio.run(m.list_tools())}["pennylane_firm_upload_urls"]


def _code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.error.data["code"]


@pytest.fixture(autouse=True)
def _etat_vide(monkeypatch):
    from oto_mcp.tools import pennylane_firm_relais as relais
    from oto_mcp.tools import pennylane_firm_socle as socle
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-banc-assez-long")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", BASE + "/")
    socle._CACHE.clear()
    relais._MANIFESTES.clear()
    yield
    socle._CACHE.clear()
    relais._MANIFESTES.clear()


@pytest.fixture
def client(monkeypatch):
    import oto.tools.pennylane_firm as pkg
    from oto_mcp.tools import pennylane_firm as P

    inst = MagicMock()
    inst.noms = {5: ["deja.pdf"], 6: []}

    def _dossiers(company_id, filter=None, **kw):
        ident = int(filter[0]["value"])
        return {"items": [{"id": ident, "name": f"d{ident}"}] if ident in DOSSIERS else [],
                "has_more": False}

    def _fichiers(company_id, parent_folder_id=None, **kw):
        return {"items": [{"id": 1, "name": n} for n in inst.noms[parent_folder_id]],
                "has_more": False}

    inst.list_dms_folders.side_effect = _dossiers
    inst.list_dms_files.side_effect = _fichiers
    monkeypatch.setattr(pkg, "PennylaneFirmClient", lambda key, **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("jeton", False))
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: ORG)
    monkeypatch.setattr(P, "current_user_sub_from_token", lambda: SUB)
    return inst


def _lot(*elements):
    return [{"parent_folder_id": d, "name": n} for d, n in elements]


def _manifeste(r) -> list[list[str]]:
    from oto_mcp.tools import pennylane_firm_relais as relais
    jeton = r["manifest_url"].rsplit("/", 1)[1]
    payload = relais.verifier_manifeste(jeton)
    e = relais._MANIFESTES[payload["target"]["manifest_id"]]
    return [[str(d), n, u] for d, n, u in e["lignes"]]


# ── Les bornes du lot ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, code", [(0, "empty_batch"), (101, "batch_too_large")])
def test_un_lot_vide_ou_au_dela_de_100_est_refuse_par_son_nom(client, n, code):
    with pytest.raises(McpError) as e:
        _outil().fn(company_id=9, items=_lot(*[(5, f"f{i}.pdf") for i in range(n)]))
    assert _code(e) == code
    client.list_dms_folders.assert_not_called()


def test_le_schema_servi_decrit_les_elements(client):
    """Ce que l'agent lit : `company_id` et `items` requis, chaque élément un objet à
    `parent_folder_id` entier et `name` ; et l'appel MCP réel les valide."""
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane_firm as P

    params = _outil().parameters
    assert set(params["required"]) == {"company_id", "items"}
    element = params["properties"]["items"]["items"]
    if "$ref" in element:
        element = params["$defs"][element["$ref"].rsplit("/", 1)[1]]
    assert set(element["required"]) == {"parent_folder_id", "name"}
    assert element["properties"]["parent_folder_id"]["type"] == "integer"
    m = FastMCP("t")
    P.register(m)
    r = asyncio.run(m.call_tool("pennylane_firm_upload_urls", {
        "company_id": 9, "items": [{"parent_folder_id": 6, "name": "a.pdf"}]}))
    sortie = r.structured_content if hasattr(r, "structured_content") else r
    assert "a.pdf" in json.dumps(sortie, default=str)


def test_100_elements_passent(client):
    r = _outil().fn(company_id=9, items=_lot(*[(6, f"f{i}.pdf") for i in range(100)]))
    assert len(r["ready"]) == 100 and r["refused"] == []


def test_la_frappe_hors_org_est_refusee(client, monkeypatch):
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: None)
    with pytest.raises(McpError) as e:
        _outil().fn(company_id=9, items=_lot((5, "a.pdf")))
    assert _code(e) == "no_organization"
    client.list_dms_folders.assert_not_called()


# ── Le chemin nominal : un manifeste, aucune URL de lien dans la réponse ──────

def test_le_lot_rend_un_manifeste_et_aucune_url_de_lien(client):
    from oto_mcp import upload_tokens
    from oto_mcp.tools import pennylane_firm as P
    from oto_mcp.tools import pennylane_firm_relais as relais

    r = _outil().fn(company_id=9, items=_lot((5, " a.pdf "), (6, "b c.pdf"),
                                             (6, "é;,x.pdf")))
    assert set(r) == {"manifest_url", "expires_at", "max_bytes", "ready", "refused",
                      "script", "hint"}
    assert r["ready"] == [{"parent_folder_id": 5, "name": "a.pdf"},
                          {"parent_folder_id": 6, "name": "b c.pdf"},
                          {"parent_folder_id": 6, "name": "é;,x.pdf"}]
    assert r["refused"] == [] and r["script"] == P.SCRIPT_LOT
    assert r["max_bytes"] == relais.max_bytes()
    assert r["manifest_url"].startswith(f"{BASE}/api/relay/batch/")
    # Tout l'objet : AUCUN lien de relais, ni en clair ni en jeton.
    texte = json.dumps(r, ensure_ascii=False)
    assert not re.search(r"/api/relay/(?!batch/)", texte)
    lignes = _manifeste(r)
    for (d, n, u), attendu in zip(lignes, r["ready"]):
        assert (int(d), n) == (attendu["parent_folder_id"], attendu["name"])
        jeton = u.rsplit("/", 1)[1]
        assert jeton not in texte
        payload = relais.verifier(jeton)
        assert payload["sub"] == SUB and payload["org"] == ORG
        assert payload["target"] == {"kind": "pennylane_firm_dms", "company_id": 9,
                                     "parent_folder_id": int(d), "name": n}
    # Le jeton du manifeste : sa famille à lui.
    jm = r["manifest_url"].rsplit("/", 1)[1]
    assert relais.verifier(jm) is None and upload_tokens.verify(jm) is None
    assert r["expires_at"] >= time.time() + relais.TTL_S - 5


def test_une_lecture_par_dossier_distinct(client):
    _outil().fn(company_id=9, items=_lot((5, "a.pdf"), (6, "b.pdf"), (5, "c.pdf"),
                                         (6, "d.pdf")))
    assert [c.kwargs["filter"] for c in client.list_dms_folders.call_args_list] == [
        [{"field": "id", "operator": "eq", "value": "5"}],
        [{"field": "id", "operator": "eq", "value": "6"}]]
    assert [c.args for c in client.list_dms_folders.call_args_list] == [(9,), (9,)]
    assert [c.kwargs["parent_folder_id"] for c in client.list_dms_files.call_args_list] \
        == [5, 6], "une lecture complète par dossier, le cache sert les suivants"


# ── Les refus par élément ──────────────────────────────────────────────────────

def test_chaque_refus_par_element_sans_faire_echouer_le_lot(client):
    r = _outil().fn(company_id=9, items=_lot(
        (5, ""), (5, "a\tb.pdf"), (6, "ligne\n.pdf"), (5, "x" * 256),
        (6, "ok.pdf"), (6, " ok.pdf"), (99, "perdu.pdf"), (5, "deja.pdf"),
        (5, "neuf.pdf")))
    assert r["ready"] == [{"parent_folder_id": 6, "name": "ok.pdf"},
                          {"parent_folder_id": 5, "name": "neuf.pdf"}]
    assert [(x["parent_folder_id"], x["code"]) for x in r["refused"]] == [
        (5, "invalid_name"), (5, "invalid_name"), (6, "invalid_name"),
        (5, "invalid_name"), (6, "duplicate_in_batch"), (99, "folder_not_found"),
        (5, "name_already_exists")]
    assert "tab" in r["refused"][1]["message"], "la raison est dite"
    assert all(x["message"] for x in r["refused"])
    assert [x["name"] for x in r["refused"]][5:] == ["perdu.pdf", "deja.pdf"]
    assert len(_manifeste(r)) == 2


def test_un_dossier_trop_grand_refuse_ses_elements_sur_une_seule_lecture(client):
    def _fichiers(company_id, parent_folder_id=None, **kw):
        return {"items": [], "has_more": parent_folder_id == 6, "next_cursor": "c"}
    client.list_dms_files.side_effect = _fichiers
    r = _outil().fn(company_id=9, items=_lot((6, "a.pdf"), (5, "b.pdf"), (6, "c.pdf")))
    assert r["ready"] == [{"parent_folder_id": 5, "name": "b.pdf"}]
    assert [(x["name"], x["code"]) for x in r["refused"]] == [
        ("a.pdf", "duplicate_check_incomplete"), ("c.pdf", "duplicate_check_incomplete")]
    assert [c.kwargs["parent_folder_id"] for c in client.list_dms_files.call_args_list] \
        == [6, 5]


def test_un_filtre_ignore_en_amont_ne_passe_pas_pour_un_dossier_trouve(client):
    client.list_dms_folders.side_effect = lambda *a, **k: {"items": [{"id": 1234}]}
    r = _outil().fn(company_id=9, items=_lot((5, "a.pdf")))
    assert [x["code"] for x in r["refused"]] == ["folder_not_found"]


def test_tout_refuse_rend_ni_manifeste_ni_lien(client):
    from oto_mcp.tools import pennylane_firm_relais as relais
    r = _outil().fn(company_id=9, items=_lot((99, "a.pdf"), (5, "deja.pdf")))
    assert r["manifest_url"] is None and r["ready"] == []
    assert len(r["refused"]) == 2 and relais._MANIFESTES == {}


# ── Une erreur qui n'est pas propre à un élément : tout l'appel échoue ─────────

@pytest.mark.parametrize("ou", ["dossiers", "fichiers"])
def test_un_429_de_pennylane_fait_echouer_tout_l_appel_sans_lien(client, monkeypatch, ou):
    from oto.tools.pennylane_firm import PennylaneFirmRateLimited
    from oto_mcp.tools import pennylane_firm_relais as relais
    frappes = []
    monkeypatch.setattr(relais, "frapper", lambda *a, **k: frappes.append(a))
    erreur = PennylaneFirmRateLimited(429, "rate_limited", "rate exceeded", retryable=True)
    if ou == "dossiers":
        client.list_dms_folders.side_effect = [
            {"items": [{"id": 5}]}, erreur]
    else:
        client.list_dms_files.side_effect = [{"items": [], "has_more": False}, erreur]
    with pytest.raises(McpError) as e:
        _outil().fn(company_id=9, items=_lot((5, "a.pdf"), (6, "b.pdf")))
    assert _code(e) == "pennylane_rate_limited"
    assert e.value.error.data["retry_after_seconds"] == 60
    assert frappes == [] and relais._MANIFESTES == {}


def test_une_societe_introuvable_fait_echouer_tout_l_appel(client):
    from oto.tools.pennylane_firm import PennylaneFirmError
    client.list_dms_folders.side_effect = PennylaneFirmError(404, "x", "no company")
    with pytest.raises(McpError) as e:
        _outil().fn(company_id=9, items=_lot((5, "a.pdf")))
    assert _code(e) == "pennylane_not_found"


# ── Le magasin des manifestes ──────────────────────────────────────────────────

def test_le_magasin_est_borne_et_refuse_au_lieu_d_evincer(monkeypatch):
    from oto_mcp.tools import pennylane_firm_relais as relais
    from oto_mcp.tools import pennylane_firm_socle as socle
    monkeypatch.setattr(relais, "MANIFESTES_MAX", 2)
    plus_tard = int(time.time()) + 900
    a = relais.deposer_manifeste(SUB, ORG, [(5, "a.pdf", "u")], plus_tard)
    relais.deposer_manifeste(SUB, ORG, [], plus_tard)
    with pytest.raises(socle.Refus) as e:
        relais.deposer_manifeste(SUB, ORG, [], plus_tard)
    assert e.value.code == "manifest_store_full" and e.value.retryable
    assert a in relais._MANIFESTES, "rien n'est évincé en silence"


def test_un_manifeste_expire_est_purge_a_l_ecriture_et_ne_se_lit_plus(monkeypatch):
    from oto_mcp import roles
    from oto_mcp.tools import pennylane_firm_relais as relais
    from oto_mcp.tools import pennylane_firm_socle as socle
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(relais, "MANIFESTES_MAX", 1)
    vieux = relais.deposer_manifeste(SUB, ORG, [(5, "a.pdf", "u")], int(time.time()) - 1)
    with pytest.raises(socle.Refus) as e:
        relais.lire_manifeste(SUB, ORG, vieux)
    assert (e.value.status, e.value.code) == (404, "manifest_expired")
    neuf = relais.deposer_manifeste(SUB, ORG, [(5, "b.pdf", "v")], int(time.time()) + 900)
    assert list(relais._MANIFESTES) == [neuf], "l'expiré est purgé, la place est rendue"
    assert relais.lire_manifeste(SUB, ORG, neuf) == (
        "parent_folder_id\tname\turl\n5\tb.pdf\tv\n")
    # Un autre compte, ou une autre org, ne lit pas ce manifeste.
    for sub, org in (("autre", ORG), (SUB, ORG + 1)):
        with pytest.raises(socle.Refus):
            relais.lire_manifeste(sub, org, neuf)


# ── La route `GET /api/relay/batch/{token}` ────────────────────────────────────

@pytest.fixture
def banc(client, monkeypatch):
    from oto_mcp import garde_identite, roles
    from oto_mcp.api import pennylane_firm as route

    etat = {"membres": {(SUB, ORG)}, "garde": None}
    monkeypatch.setattr(garde_identite, "refus", lambda sub: etat["garde"])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: (sub, org) in etat["membres"])
    app = Starlette(routes=[
        Route("/api/relay/{token}", route.relay_receive, methods=["POST"]),
        Route("/api/relay/batch/{token}", route.relay_manifest, methods=["GET"])])
    etat["http"] = TestClient(app)
    etat["lot"] = _outil().fn(company_id=9, items=_lot((5, "a b;c.pdf"), (6, "é,d.pdf")))
    etat["chemin"] = etat["lot"]["manifest_url"].replace(BASE, "")
    return etat


def test_la_route_sert_le_manifeste_en_tsv_exact_et_relisible(banc):
    r = banc["http"].get(banc["chemin"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/tab-separated-values; charset=utf-8"
    lignes = r.text.split("\n")
    assert lignes[0] == "parent_folder_id\tname\turl" and lignes[-1] == ""
    corps = [ligne.split("\t") for ligne in lignes[1:-1]]
    assert [(d, n) for d, n, _ in corps] == [("5", "a b;c.pdf"), ("6", "é,d.pdf")]
    assert all(u.startswith(f"{BASE}/api/relay/") and "/batch/" not in u
               for _, _, u in corps)
    assert r.text == "parent_folder_id\tname\turl\n" + "".join(
        f"{d}\t{n}\t{u}\n" for d, n, u in _manifeste(banc["lot"]))
    assert banc["http"].get(banc["chemin"]).text == r.text, "relisible pendant le TTL"


def test_les_familles_de_jetons_ne_se_croisent_pas(banc):
    from oto_mcp import upload_tokens
    from oto_mcp.tools import pennylane_firm_relais as relais
    jeton_lot = banc["chemin"].rsplit("/", 1)[1]
    lien = _manifeste(banc["lot"])[0][2]
    jeton_relais = lien.rsplit("/", 1)[1]
    jeton_upload, _ = upload_tokens.sign(SUB, ORG, {"kind": "pennylane_firm_manifest",
                                                    "manifest_id": "x"})
    for jeton in (jeton_relais, jeton_upload):
        r = banc["http"].get(f"/api/relay/batch/{jeton}")
        assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"
    # Le jeton du manifeste n'ouvre ni le relais ni l'upload oto.
    r = banc["http"].post(f"/api/relay/{jeton_lot}", files={"file": ("a", b"x")})
    assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"
    assert upload_tokens.verify(jeton_lot) is None
    assert relais.verifier(jeton_lot) is None


def test_un_compte_sorti_de_l_org_est_refuse(banc):
    banc["membres"].clear()
    r = banc["http"].get(banc["chemin"])
    assert r.status_code == 403 and r.json()["error"] == "not_an_org_member"


def test_un_compte_coupe_par_la_garde_d_identite_est_refuse(banc):
    banc["garde"] = ("account_suspended", "suspendu")
    r = banc["http"].get(banc["chemin"])
    assert r.status_code == 403 and r.json()["error"] == "account_suspended"


def test_un_manifeste_perdu_rend_404_et_dit_de_refrapper(banc):
    from oto_mcp.tools import pennylane_firm_relais as relais
    relais._MANIFESTES.clear()   # un redémarrage, ou le TTL
    r = banc["http"].get(banc["chemin"])
    assert r.status_code == 404 and r.json()["error"] == "manifest_expired"
    assert "mint the batch again" in r.json()["detail"]


def test_l_ordre_des_routes_servies_et_le_masque_du_journal():
    """Sur la table SERVIE : `GET /api/relay/batch/x` arrive au manifeste, `GET
    /api/relay/x` n'y arrive pas (le relais ne connaît que POST), et le jeton du
    chemin est masqué au journal comme celui du relais."""
    from oto_mcp import journal_secrets
    from oto_mcp.api import routes as api_routes

    http = TestClient(Starlette(routes=api_routes.make_routes(object())))
    r = http.get("/api/relay/batch/x")
    assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"
    assert http.get("/api/relay/x").status_code == 405
    assert http.post("/api/relay/batch/x").status_code == 405
    route, masques = journal_secrets.route_and_secrets("/api/relay/batch/abc.def")
    assert route == "/api/relay/batch/:token" and "abc.def" not in repr(masques)
    assert journal_secrets.route_and_secrets("/api/relay/abc.def")[0] == "/api/relay/:token"


# ── Le script fourni, joué par bash ───────────────────────────────────────────

_FAUX_CURL = r"""#!/usr/bin/env bash
out=""; url=""; form=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) shift 2;;
    -F) form="$2"; shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
if [ "$url" = "$FAUX_MANIFESTE_URL" ]; then
  cat "$FAUX_MANIFESTE" > "$out"; printf '%s' "${FAUX_MANIFESTE_CODE:-200}"; exit 0
fi
printf '%s\037%s\n' "$url" "$form" >> "$FAUX_JOURNAL"
case "$url" in *conflit*) printf 409;; *) printf 200;; esac
"""

_FAUX_XARGS = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAUX_XARGS_JOURNAL"
exec "$VRAI_XARGS" "$@"
"""


def _executable(chemin: Path, texte: str) -> None:
    chemin.write_text(texte, encoding="utf-8")
    chemin.chmod(chemin.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def poste(tmp_path):
    from oto_mcp.tools import pennylane_firm as P

    if not (shutil.which("bash") and shutil.which("awk") and shutil.which("xargs")):
        pytest.skip("bash, awk et xargs requis")
    faux = tmp_path / "bin"
    faux.mkdir()
    _executable(faux / "curl", _FAUX_CURL)
    _executable(faux / "xargs", _FAUX_XARGS)
    script = tmp_path / "pennylane_batch.sh"
    script.write_text(P.SCRIPT_LOT, encoding="utf-8")
    env = {**os.environ, "PATH": f"{faux}:{os.environ['PATH']}",
           "VRAI_XARGS": shutil.which("xargs"),
           "FAUX_MANIFESTE_URL": f"{BASE}/api/relay/batch/jeton-du-lot",
           "FAUX_MANIFESTE": str(tmp_path / "manifeste.tsv"),
           "FAUX_JOURNAL": str(tmp_path / "envois.log"),
           "FAUX_XARGS_JOURNAL": str(tmp_path / "xargs.log")}
    return {"dir": tmp_path, "script": script, "env": env}


def _jouer(poste, manifeste: str, paths: str):
    d = poste["dir"]
    (d / "manifeste.tsv").write_text(manifeste, encoding="utf-8")
    (d / "paths.tsv").write_text(paths, encoding="utf-8")
    return subprocess.run(
        ["bash", str(poste["script"]), poste["env"]["FAUX_MANIFESTE_URL"],
         str(d / "paths.tsv"), str(d / "results.tsv")],
        cwd=d, env=poste["env"], capture_output=True, text=True, timeout=60)


def test_le_script_joint_envoie_sur_4_flux_et_signale_no_link(poste):
    noms = ["a b.pdf", "é;x.pdf", "c,d.pdf", "f;g, h é.pdf", "conflit.pdf"]
    manifeste = "parent_folder_id\tname\turl\n" + "".join(
        f"{5 + i % 2}\t{n}\t{BASE}/api/relay/L{i}{'-conflit' if 'conflit' in n else ''}\n"
        for i, n in enumerate(noms))
    chemins = {n: f"/données/lot 1/{n}" for n in noms}
    paths = "".join(f"{5 + i % 2}\t{n}\t{chemins[n]}\n" for i, n in enumerate(noms))
    paths += "5\trefusé.pdf\t/données/refusé.pdf\n"         # absent du manifeste
    paths += "5\ta b.pdf\t/données/copie de a b.pdf\r\n"    # déjà servi : un lien, un envoi
    paths += "6\ta b.pdf\t/données/autre dossier.pdf\n"     # même nom, autre dossier
    r = _jouer(poste, manifeste, paths)
    assert r.returncode == 0, r.stderr

    envois = [ligne.split("\x1f") for ligne in
              (poste["dir"] / "envois.log").read_text(encoding="utf-8").splitlines()]
    assert sorted(envois) == sorted(
        [f"{BASE}/api/relay/L{i}{'-conflit' if 'conflit' in n else ''}",
         f'file=@"{chemins[n]}"'] for i, n in enumerate(noms))

    lignes = (poste["dir"] / "results.tsv").read_text(encoding="utf-8").splitlines()
    assert lignes[0] == "code_http\tname\tpath"
    assert sorted(ligne.split("\t") for ligne in lignes[1:]) == sorted(
        [["200" if n != "conflit.pdf" else "409", n, chemins[n]] for n in noms]
        + [["NO_LINK", "refusé.pdf", "/données/refusé.pdf"],
           ["NO_LINK", "a b.pdf", "/données/copie de a b.pdf"],
           ["NO_LINK", "a b.pdf", "/données/autre dossier.pdf"]])

    # Le journal du faux xargs : ses options, puis le corps de `bash -c` (multiligne).
    journal_xargs = (poste["dir"] / "xargs.log").read_text()
    assert journal_xargs.count(" bash -c ") == 1, "un seul xargs, qui répartit"
    options = journal_xargs.split(" bash -c ")[0].split()
    assert options[options.index("-P") + 1] == "4", "4 flux au plus"
    assert "-0" in options, "séparateurs NUL"


def test_le_script_echappe_guillemet_et_barre_oblique_inverse(poste):
    manifeste = f"parent_folder_id\tname\turl\n5\tq.pdf\t{BASE}/api/relay/Q\n"
    _jouer(poste, manifeste, '5\tq.pdf\t/tmp/a "b" \\c.pdf\n')
    (url, form), = [ligne.split("\x1f") for ligne in
                    (poste["dir"] / "envois.log").read_text().splitlines()]
    assert form == 'file=@"/tmp/a \\"b\\" \\\\c.pdf"'


def test_le_script_s_arrete_si_le_manifeste_est_perdu(poste):
    poste["env"]["FAUX_MANIFESTE_CODE"] = "404"
    r = _jouer(poste, '{"error":"manifest_expired"}', "5\ta.pdf\t/x/a.pdf\n")
    assert r.returncode == 1 and "manifest_expired" in r.stderr
    assert not (poste["dir"] / "envois.log").exists()
