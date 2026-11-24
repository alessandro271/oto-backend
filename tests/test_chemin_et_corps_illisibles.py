"""Un texte que PostgreSQL ne peut pas porter rend un 400 NOMMÉ, jamais un
`500 internal_error` — dans le CHEMIN et dans le CORPS JSON, après la query string
(`test_query_valeurs_illisibles.py`). Durcissement CASA 6.2.1, trouvé par un scan DAST
authentifié.

Servi par la vraie table de routes (`make_routes` + `TestClient`) contre un vrai
PostgreSQL : un `%00` doit survivre au routage pour que l'épreuve dise quelque chose.

1. **Chemin** : `/api/datastores/{n}/rows/%00/claim` — le `row_id` partait au SQL et
   le pilote levait (« text fields cannot contain NUL »). Refusé `400 invalid_path`
   par l'adaptateur, avant tout handler et avant le tampon `deprecations` (qui lit la
   base avec l'adresse du chemin quand il est allumé).
2. **Corps** : `{"worker": "a\\u0000b"}` sur la réservation, et la même panne sur
   `claim_next` (worker, `filter`, `filters`), la libération, le commentaire,
   l'écriture d'une ligne ; plus une moitié de paire de substitution seule
   (`"\\ud800"`, ne s'encode pas en UTF-8). Refusé `400 invalid_body` par
   `json_body.read_json_body`, le seam unique du corps.
3. Les routes du scan que #1962 a déjà fermées (`guide-library`, `me/calls`,
   `comments/counts`) : leurs entrées exactes, gardées ici en régression.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import json_body

A = "sub-chemin-corps"
H = {"Authorization": f"Bearer {A}"}
JSON = {**H, "content-type": "application/json"}


class _Claims:
    def __init__(self, sub):
        self.claims = {"sub": sub, "email": f"{sub}@chemin-corps.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token):
        return _Claims(token)


# ── Sans base : le juge du corps ──────────────────────────────────────────────


@pytest.mark.parametrize("corps, chemin", [
    ({"worker": "a\x00b"}, "worker"),
    ({"filter": {"t": "\x00"}}, "filter.t"),
    ({"filters": [{"field": "t", "value": "\x00"}]}, "filters[0].value"),
    ({"filter": {"\x00": "a"}}, "filter.<key>"),
    ({"\x00": "a"}, "<key>"),
    ({"t": "\ud800"}, "t"),
    ({"t": "ok"}, None),
    ({"t": "\U0001F600", "n": [1, None, True]}, None),
])
def test_le_corps_nomme_la_chaine_impossible(corps, chemin):
    assert json_body._texte_impossible(corps) == chemin


# ── Les routes servies ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(live):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp import db
    from oto_mcp.api import routes as api_routes
    db.upsert_user(A, email=f"{A}@chemin-corps.invalid", name=A)
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app), raise_server_exceptions=False)


@pytest.fixture
def ligne(client):
    """Un tableau, une ligne, un commentaire : les lectures ont de quoi parcourir."""
    from oto_mcp import db
    ns_id = db.create_datastore("user", A, f"chemin-corps-{uuid.uuid4().hex[:6]}")
    db.set_datastore_schema(ns_id, {"fields": [{"key": "t", "type": "text"}]})
    r = client.post(f"/api/datastores/{ns_id}/rows", headers=H, json={"t": "x"})
    assert r.status_code == 201, r.text
    rid = r.json()["_id"]
    r = client.post(f"/api/datastores/{ns_id}/rows/{rid}/comments", headers=H,
                    json={"body": "premier"})
    assert r.status_code == 201, r.text
    return ns_id, rid


_CHEMIN = (400, "invalid_path")
_CORPS = (400, "invalid_body")
_QUERY = (400, "invalid_query")

# (verbe, url — `{ns}`/`{rid}` remplacés —, corps brut ou None, attendu)
CAS = [
    # La route du scan, et la valeur qui la faisait tomber.
    ("POST", "/api/datastores/{ns}/rows/%00/claim", b'{"worker":"w"}', _CHEMIN),
    ("POST", "/api/datastores/{ns}/rows/a%00b/claim", b'{"worker":"w"}', _CHEMIN),
    ("POST", "/api/datastores/{ns}/rows/{rid}/claim", b'{"worker":"a\\u0000b"}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows/{rid}/claim", b'{"worker":"\\ud800"}', _CORPS),
    # Les jumelles de la même famille (chemin).
    ("GET", "/api/datastores/%00/rows", None, _CHEMIN),
    ("GET", "/api/datastores/{ns}/rows/%00", None, _CHEMIN),
    ("POST", "/api/datastores/{ns}/rows/%00/release", b'{"worker":"w"}', _CHEMIN),
    ("GET", "/api/datastores/{ns}/rows/%00/comments", None, _CHEMIN),
    ("POST", "/api/datastores/{ns}/rows/%00/comments", b'{"body":"x"}', _CHEMIN),
    # … et (corps).
    ("POST", "/api/datastores/{ns}/claim_next", b'{"worker":"a\\u0000b"}', _CORPS),
    ("POST", "/api/datastores/{ns}/claim_next",
     b'{"worker":"w","filter":{"t":"a\\u0000"}}', _CORPS),
    ("POST", "/api/datastores/{ns}/claim_next",
     b'{"worker":"w","filters":[{"field":"t","op":"eq","value":"\\u0000"}]}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows/{rid}/release", b'{"worker":"a\\u0000b"}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows/{rid}/comments", b'{"body":"a\\u0000b"}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows", b'{"t":"a\\u0000b"}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows", b'{"t":"\\ud800"}', _CORPS),
    # `json.loads` lit aussi un substitut UTF-8 BRUT et un corps UTF-16 : le juge porte
    # sur le corps décodé, pas sur les octets.
    ("POST", "/api/datastores/{ns}/rows", b'{"t":"\xed\xa0\x80"}', _CORPS),
    ("POST", "/api/datastores/{ns}/rows", '{"t":"a\\u0000b"}'.encode("utf-16"), _CORPS),
    # Ce qui doit rester servi : une paire complète, un antislash échappé.
    ("POST", "/api/datastores/{ns}/rows/{rid}/claim", b'{"worker":"w\\ud83d\\ude00"}', 200),
    ("POST", "/api/datastores/{ns}/rows", b'{"t":"\\\\u0000"}', 201),
    # Déjà fermées par #1962 (NUL de query, `days` écrêté) : les entrées du scan.
    ("GET", "/api/me/guide-library?query=%00", None, _QUERY),
    ("GET", "/api/me/guide-library?category=a%00b", None, _QUERY),
    ("GET", "/api/me/guide-library?author_kind=%00", None, _QUERY),
    ("GET", "/api/me/guide-library?query=%25%5C_&limit=-5", None, 200),
    ("GET", "/api/me/calls?days=99999999999999999999", None, 200),
    ("GET", "/api/me/calls?days=2147483648", None, 200),
    ("GET", "/api/me/calls?days=-99999999999999999999", None, 200),
    ("GET", "/api/me/calls?tool=%00", None, _QUERY),
    ("GET", "/api/datastores/{ns}/comments/counts?row_ids=%00", None, _QUERY),
    ("GET", "/api/datastores/{ns}/comments/counts?row_ids={rid}&row_ids=a%00b", None,
     _QUERY),
    ("GET", "/api/datastores/{ns}/comments/counts?row_ids={rid},%2C,", None, 200),
]


@pytest.mark.parametrize("compat", ["0", "1"])
@pytest.mark.parametrize("verbe, url, corps, attendu", CAS,
                         ids=[f"{c[0]} {c[1]} {c[2]!r}" for c in CAS])
def test_la_route_servie_ne_rend_jamais_un_500(client, ligne, monkeypatch, compat,
                                               verbe, url, corps, attendu):
    # Le tampon « tableau par nom » allumé lit la base avec l'adresse du chemin :
    # la garde doit passer avant lui, donc l'épreuve tourne avec et sans.
    monkeypatch.setenv("OTO_MCP_COMPAT_TABLEAU_PAR_NOM", compat)
    ns, rid = ligne
    url = url.replace("{ns}", str(ns)).replace("{rid}", rid)
    kw = {"headers": H} if corps is None else {"headers": JSON, "content": corps}
    r = client.request(verbe, url, **kw)
    if isinstance(attendu, int):
        assert r.status_code == attendu, r.text
        return
    assert (r.status_code, r.json().get("error")) == attendu, r.text
