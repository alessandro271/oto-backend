"""Gate d'acceptation légale (`me.legal`) — composition du statut + accept.

Stub de `db.get/record_legal_acceptances` (convention repo : pas de vrai PG en unit).
Le roundtrip SQL réel (l'historique, la lecture de la ligne la plus récente, la
migration vivante) est prouvé contre un vrai PostgreSQL dans
`test_legal_acceptance_history_487.py` — ici on couvre la LOGIQUE : outstanding par
contexte, accept, contexte inconnu.

⚠️ Le stub SIMULE UN HISTORIQUE (une ligne par acceptation, lecture = la plus
récente), pas un état : un stub qui écraserait rendrait vertes des lectures que la
vraie table ne sert plus.
"""
import pytest

from oto_mcp import db, legal_docs, tenancy
from _documents_legaux import bumper
from oto_mcp.capabilities import me_legal
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def store(monkeypatch):
    """Un historique en mémoire : `lignes` s'allonge, la lecture prend la dernière."""
    lignes: list[dict] = []

    def _get(sub):
        dernieres: dict[str, dict] = {}
        for ligne in lignes:
            if ligne["sub"] == sub:
                dernieres[ligne["doc_slug"]] = {"version": ligne["version"],
                                                "accepted_at": ligne["accepted_at"]}
        return dernieres

    def _record(sub, items, *, context=None, org_id=None, ip=None, user_agent=None):
        for slug, version in items:
            lignes.append({"sub": sub, "doc_slug": slug, "version": version,
                           "accepted_at": "2026-07-09 10:00:00", "context": context,
                           "org_id": org_id, "ip": ip, "user_agent": user_agent})

    monkeypatch.setattr(db, "get_legal_acceptances", _get)
    monkeypatch.setattr(db, "record_legal_acceptances", _record)
    return lignes


def _ctx():
    return ResolvedCtx(sub="s1", org_id=None, role="member")


def test_initial_all_outstanding(store):
    st = me_legal._status("s1", tenancy.primary_slug())
    assert st["contexts"]["access"]["outstanding"] == ["terms"]
    assert st["contexts"]["purchase"]["outstanding"] == ["terms", "cgv", "dpa"]
    assert all(not d["accepted"] for d in st["documents"])


def test_accept_access_clears_access_only(store):
    st = me_legal._accept(_ctx(), me_legal.AcceptInput(context="access"))
    assert st["contexts"]["access"]["outstanding"] == []
    assert st["contexts"]["purchase"]["outstanding"] == ["cgv", "dpa"]
    terms = next(d for d in st["documents"] if d["slug"] == "terms")
    assert terms["accepted"] and terms["accepted_version"] == legal_docs.current_docs()["terms"]["version"]


def test_accept_purchase_clears_all(store):
    me_legal._accept(_ctx(), me_legal.AcceptInput(context="purchase"))
    st = me_legal._status("s1", tenancy.primary_slug())
    assert st["contexts"]["purchase"]["outstanding"] == []


def test_version_bump_reopens(store, monkeypatch):
    me_legal._accept(_ctx(), me_legal.AcceptInput(context="access"))
    bumper(monkeypatch, "terms", "999.0")
    st = me_legal._status("s1", tenancy.primary_slug())
    assert st["contexts"]["access"]["outstanding"] == ["terms"]


def test_unknown_context_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        me_legal._accept(_ctx(), me_legal.AcceptInput(context="bogus"))
    assert e.value.status == 400 and e.value.code == "unknown_context"


def test_accept_situe_l_acte_et_n_ecrase_jamais(store):
    """Ce que la trace doit porter en plus de la version : le contexte et l'org de
    session — et deux acceptations font DEUX lignes, pas une remplacée."""
    ctx = ResolvedCtx(sub="s1", org_id=77, role="member")
    me_legal._accept(ctx, me_legal.AcceptInput(context="access"))
    me_legal._accept(ctx, me_legal.AcceptInput(context="purchase"))

    terms = [l for l in store if l["doc_slug"] == "terms"]
    assert len(terms) == 2, "la seconde acceptation s'ajoute, elle ne remplace pas"
    assert [l["context"] for l in terms] == ["access", "purchase"]
    assert {l["org_id"] for l in store} == {77}


# ── l'empreinte du client : elle vient de la REQUÊTE, donc on passe par la route ──
#
# `client_trace` est posé par l'adaptateur REST : un appel direct au handler ne peut
# pas le prouver (il rendrait deux `None`, et le test serait d'accord avec lui-même).

_BOX = ("127.0.0.1", 50000)   # le proxy de la box, pair TCP du backend


def _proxies(monkeypatch, valeur="127.0.0.1/32,::1/128"):
    """`OTO_PROXIES_DE_CONFIANCE` (#1232) ; `None` = réglage absent."""
    from oto_mcp import client_trace
    if valeur is None:
        monkeypatch.delenv("OTO_PROXIES_DE_CONFIANCE", raising=False)
    else:
        monkeypatch.setenv("OTO_PROXIES_DE_CONFIANCE", valeur)
    monkeypatch.setattr(client_trace, "_RESEAUX", None)


def _accepte_par_la_route(monkeypatch, headers, contexte="purchase", client=_BOX):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch, org_id=219)
    return call("me.legal.accept", body={"context": contexte}, headers=headers,
                client=client)


def test_l_ip_reelle_est_celle_de_cloudflare_pas_celle_du_relais(store, monkeypatch):
    _proxies(monkeypatch)
    code, corps = _accepte_par_la_route(monkeypatch, [
        (b"cf-connecting-ip", b"203.0.113.7"),
        (b"x-forwarded-for", b"198.51.100.9, 10.0.0.1"),
        (b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) Firefox/141.0"),
    ])
    assert code == 200
    assert {l["ip"] for l in store} == {"203.0.113.7"}
    assert all("Firefox" in l["user_agent"] for l in store)
    assert {l["context"] for l in store} == {"purchase"}
    assert {l["org_id"] for l in store} == {219}, "l'org de session = le payeur"
    # …et la réponse dit ce qui manque ENCORE — ici plus rien.
    assert corps["contexts"]["purchase"]["outstanding"] == []


def test_sans_cloudflare_c_est_le_saut_le_plus_a_droite_hors_des_relais(store, monkeypatch):
    """La liste se lit client → relais, chaque relais ajoutant en queue : on remonte
    depuis la droite les relais déclarés de confiance (ici un second, `10.0.0.0/8`), et
    le premier saut qui n'en est pas un est le client. Plus à gauche, c'est le client
    qui a écrit."""
    _proxies(monkeypatch, "127.0.0.1/32,10.0.0.0/8")
    _accepte_par_la_route(monkeypatch, [
        (b"x-forwarded-for", b"192.0.2.66, 198.51.100.9, 10.0.0.1"),
    ], contexte="access")
    assert {l["ip"] for l in store} == {"198.51.100.9"}


def test_sans_proxy_declare_les_en_tetes_ne_situent_rien(store, monkeypatch):
    """#1232 : sans `OTO_PROXIES_DE_CONFIANCE`, un en-tête d'adresse est écrit par qui
    veut — la trace porte le pair TCP, jamais une adresse choisie par le client."""
    _proxies(monkeypatch, None)
    _accepte_par_la_route(monkeypatch, [
        (b"cf-connecting-ip", b"203.0.113.7"),
        (b"x-forwarded-for", b"198.51.100.9"),
    ], contexte="access", client=("192.0.2.44", 50000))
    assert {l["ip"] for l in store} == {"192.0.2.44"}


def test_une_requete_sans_en_tete_trace_des_nuls_pas_des_inventions(store, monkeypatch):
    _accepte_par_la_route(monkeypatch, [], contexte="access", client=None)
    assert {l["ip"] for l in store} == {None}
    assert {l["user_agent"] for l in store} == {None}
