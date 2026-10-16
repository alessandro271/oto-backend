"""Connecteur Modjo — les cinq outils `modjo_*` (deux modules et leur socle).

Les tripwires génériques couvrent le registre, l'éditeur, le logo, la prose
servie et la jointure au client de la bibliothèque. Ce fichier verrouille ce qui
est PROPRE au connecteur, par le VRAI chemin FastMCP (`mcp.call_tool`), le client
remplacé par un double en mémoire :

- une liste n'étend que des relations légères ; le transcript ne passe que par
  `op="transcript"`, en tranches ;
- la pagination dite honnêtement : `next_page` calculé depuis `page * size < total` ;
- l'import d'un appel rend un APERÇU tant que `confirm=True` n'est pas passé —
  aucun appel d'écriture ne part ; l'aperçu dit si un participant `user` est membre ;
- les arguments qu'un op n'utilise pas, refusés ;
- la traduction des refus amont (4xx → refus nommé ; 429 et 5xx restent typés).
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp import access
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import modjo as M
from oto_mcp.tools import modjo_annuaire as MA


class _FauxClient:
    """Double en mémoire du client de la bibliothèque : rend des réponses, note les appels."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.appels = []
        self.leve = None
        self.reponses = {}

    def _note(self, nom, *args, **kw):
        self.appels.append((nom, args, kw))
        if self.leve:
            raise self.leve
        return self.reponses.get(nom, {})

    def __getattr__(self, nom):
        if nom.startswith("_"):
            raise AttributeError(nom)
        return lambda *a, **kw: self._note(nom, *a, **kw)


class _Banc:
    prepare = None


@pytest.fixture
def banc(monkeypatch):
    b = _Banc()
    b.construits = []
    monkeypatch.setattr(access, "resolve_api_key",
                        lambda provider, account=None, units=1: ("mj_test", False))
    import oto.tools.modjo as pkg

    def _construire(api_key=None, **kw):
        c = _FauxClient(api_key)
        if b.prepare:
            b.prepare(c)
        b.construits.append(c)
        return c

    monkeypatch.setattr(pkg, "ModjoClient", _construire)
    b.mcp = FastMCP("banc-modjo")
    M.register(b.mcp)
    MA.register(b.mcp)
    return b


def _appeler(b, outil, **arguments):
    return asyncio.run(b.mcp.call_tool(outil, arguments)).structured_content


def _appels(b):
    return [a for c in b.construits for a in c.appels]


def _refus(b, outil, **arguments) -> str:
    with pytest.raises(Exception) as e:
        _appeler(b, outil, **arguments)
    return str(e.value)


def _rendre(nom, valeur):
    def prepare(c):
        c.reponses[nom] = valeur
    return prepare


# --- appels -------------------------------------------------------------------

def test_la_liste_passe_ses_filtres_et_dit_la_page_suivante(banc):
    banc.prepare = _rendre("list_calls", {"data": [{"id": 1}],
                                          "pagination": {"page": 1, "size": 25, "total": 26}})
    out = _appeler(banc, "modjo_calls", user_id=42, date_from="2026-10-05T00:00:00Z",
                   expand=["contacts", "deal"])
    assert out["pagination"]["next_page"] == 2
    nom, _, kw = _appels(banc)[0]
    assert nom == "list_calls"
    assert kw["user_id"] == 42 and kw["date_from"] == "2026-10-05T00:00:00Z"
    assert kw["expand"] == ["contacts", "deal"]


@pytest.mark.parametrize("donnee,envoyee", [
    ("2026-10-05T08:00:00+02:00", "2026-10-05T06:00:00Z"),
    ("2026-10-05T06:00:00Z", "2026-10-05T06:00:00Z"),
    ("2026-10-05T06:00:00.250-01:00", "2026-10-05T07:00:00.250Z"),
    ("2026-10-05", "2026-10-05T00:00:00Z"),
])
def test_une_date_part_en_utc_z_modjo_refuse_un_decalage(banc, donnee, envoyee):
    """Modjo refuse `+02:00` (400) et n'accepte que `Z` : l'instant est converti."""
    _appeler(banc, "modjo_calls", date_from=donnee, created_to=donnee)
    _, _, kw = _appels(banc)[0]
    assert kw["date_from"] == envoyee and kw["created_to"] == envoyee


def test_une_date_heure_sans_fuseau_est_refusee(banc):
    msg = _refus(banc, "modjo_calls", date_to="2026-10-05T08:00:00")
    assert "date_to" in msg and "no time zone" in msg
    assert "not an ISO 8601" in _refus(banc, "modjo_reviews",
                                       start_date_time="05/10/2026")
    assert _appels(banc) == []


def test_revues_et_import_en_utc(banc):
    _appeler(banc, "modjo_reviews", start_date_time="2026-10-01T00:00:00+02:00")
    _appeler(banc, "modjo_calls", **{**_IMPORT, "date": "2026-10-12T11:30:00+02:00"},
             confirm=True)
    revues, imp = _appels(banc)
    assert revues[2]["start_date_time"] == "2026-09-30T22:00:00Z"
    assert imp[1][1] == "2026-10-12T09:30:00Z"


def test_la_derniere_page_n_a_pas_de_suivante(banc):
    banc.prepare = _rendre("list_calls", {"data": [], "pagination": {"page": 2, "size": 25, "total": 50}})
    assert _appeler(banc, "modjo_calls")["pagination"]["next_page"] is None


@pytest.mark.parametrize("lourd", ["summaries", "notes", "next-steps", "topics"])
def test_une_liste_n_etend_pas_les_corps(banc, lourd):
    assert "op='get'" in _refus(banc, "modjo_calls", expand=[lourd])
    assert _appels(banc) == []


def test_le_detail_etend_les_sous_ressources(banc):
    _appeler(banc, "modjo_calls", op="get", call_id="7", expand=["summaries", "next-steps"])
    assert _appels(banc)[0] == ("get_call", ("7",), {"expand": ["summaries", "next-steps"]})


def test_un_argument_hors_op_est_refuse(banc):
    assert "does not use `user_id`" in _refus(banc, "modjo_calls", op="get", call_id="7", user_id=1)
    assert "requires `call_id`" in _refus(banc, "modjo_calls", op="notes")
    assert _appels(banc) == []


def test_le_transcript_se_lit_en_lignes_et_par_tranches(banc, monkeypatch):
    monkeypatch.setattr(M, "TRANSCRIPT_MAX_CHARS", 50)
    blocs = [{"startTime": 65, "endTime": 70, "content": "Bonjour à tous",
              "speaker": {"id": 1, "name": "Jane", "type": "user"}},
             {"startTime": 71, "endTime": 80, "content": "Merci de nous recevoir",
              "speaker": {"id": 2, "name": None, "type": "contact"}}]
    banc.prepare = _rendre("get_call_transcript", {"data": blocs})
    out = _appeler(banc, "modjo_calls", op="transcript", call_id="7")
    assert out["transcript"].startswith("01:05 Jane (user): Bonjour à tous")
    assert out["next_offset"] == 50 and out["blocks"] == 2
    suite = _appeler(banc, "modjo_calls", op="transcript", call_id="7", offset=50)
    assert "speaker 2 (contact)" in out["transcript"] + suite["transcript"]
    assert "next_offset" not in suite


def test_un_transcript_vide_dit_pourquoi(banc):
    banc.prepare = _rendre("get_call_transcript", {"data": []})
    assert "processing" in _appeler(banc, "modjo_calls", op="transcript", call_id="7")["note"]


def test_le_transcript_json_trop_lourd_est_refuse(banc, monkeypatch):
    monkeypatch.setattr(M, "TRANSCRIPT_MAX_CHARS", 10)
    banc.prepare = _rendre("get_call_transcript", {"data": [{"content": "x" * 50}]})
    assert "by slices" in _refus(banc, "modjo_calls", op="transcript", call_id="7", full=True)


def test_tags_d_un_appel(banc):
    _appeler(banc, "modjo_calls", op="add_tag", call_id="7", tag_id=3)
    _appeler(banc, "modjo_calls", op="remove_tag", call_id="7", tag_id=3)
    assert [a[:2] for a in _appels(banc)] == [("add_call_tag", ("7", 3)), ("remove_call_tag", ("7", 3))]


# --- import : aperçu d'abord ---------------------------------------------------

_IMPORT = dict(op="upload", media_url="https://storage.example.com/a.mp3",
               date="2026-10-12T09:30:00Z",
               participants=[{"email": "jane@company.example", "type": "user"},
                             {"email": "john@customer.example", "type": "contact"}])


def test_l_import_sans_confirm_n_ecrit_rien(banc):
    banc.prepare = _rendre("list_users", {"data": [{"id": 1}]})
    out = _appeler(banc, "modjo_calls", **_IMPORT)
    assert out["dry_run"] is True
    assert out["user_participants_found"] == {"jane@company.example": True}
    assert [a[0] for a in _appels(banc)] == ["list_users"]


def test_l_apercu_previent_quand_aucun_user_n_est_membre(banc):
    banc.prepare = _rendre("list_users", {"data": []})
    assert "will refuse the import" in _appeler(banc, "modjo_calls", **_IMPORT)["note"]


def test_dry_run_l_emporte_sur_confirm(banc):
    _appeler(banc, "modjo_calls", **_IMPORT, confirm=True, dry_run=True)
    assert "upload_call" not in [a[0] for a in _appels(banc)]


def test_l_import_confirme_part(banc):
    _appeler(banc, "modjo_calls", **_IMPORT, confirm=True, crm_activity_id="act-1")
    nom, args, kw = _appels(banc)[-1]
    assert nom == "upload_call" and args[0] == _IMPORT["media_url"]
    assert kw["crm_activity_id"] == "act-1"


# --- annuaire ------------------------------------------------------------------

def test_crm_et_workspace(banc):
    _appeler(banc, "modjo_crm", op="deals", account_id=4, status="Open")
    _appeler(banc, "modjo_crm", op="deal_summary", deal_id=9)
    _appeler(banc, "modjo_workspace", op="users", email="jane@company.example")
    _appeler(banc, "modjo_workspace", op="team_members", team_id=3, page=2)
    _appeler(banc, "modjo_reviews", order="asc")
    assert [a[0] for a in _appels(banc)] == [
        "list_deals", "get_deal_summary", "list_users", "list_team_members", "list_call_reviews"]
    assert "does not use `email`" in _refus(banc, "modjo_workspace", op="tags", email="x@y.z")


_DEAL = {"id": 7, "name": "Renouvellement", "crmId": "d-1", "crmIdentifier": "d-1",
         "crmLink": "https://crm.example/deals/d-1", "crm": "hubspot", "status": "Open",
         "stage": "Proposal", "amount": 1200, "currency": "EUR", "accountId": 4,
         "ownerId": 2, "createdOn": "2026-10-01T00:00:00Z",
         "modifiedOn": "2026-10-02T00:00:00Z", "champ_de_demain": "servi"}
_PAGE = {"page": 1, "size": 25, "total": 1}


def test_une_liste_crm_rend_l_index_et_full_le_brut(banc):
    """ADR 0047 : le défaut retire les clés nommées (dates, lien et identifiant CRM
    servi deux fois) ; un champ que Modjo ajoute demain passe ; `full=True` rend tout."""
    banc.prepare = _rendre("list_deals", {"data": [dict(_DEAL)], "pagination": _PAGE})
    (deal,) = _appeler(banc, "modjo_crm", op="deals")["data"]
    assert deal == {"id": 7, "name": "Renouvellement", "crmId": "d-1", "crm": "hubspot",
                    "status": "Open", "stage": "Proposal", "amount": 1200,
                    "currency": "EUR", "accountId": 4, "ownerId": 2,
                    "champ_de_demain": "servi"}
    assert _appeler(banc, "modjo_crm", op="deals", full=True)["data"] == [_DEAL]


def test_un_objet_seul_est_servi_entier_et_refuse_full(banc):
    assert "does not use `full`" in _refus(banc, "modjo_crm", op="account", account_id=4,
                                           full=True)
    assert "does not use `full`" in _refus(banc, "modjo_workspace", op="user", user_id=2,
                                           full=True)


def test_les_membres_sans_telephone_ni_fuseau(banc):
    banc.prepare = _rendre("list_users", {"data": [{
        "id": 2, "email": "jane@company.example", "firstName": "Jane", "lastName": "Doe",
        "phoneNumber": "+33100000000", "jobTitle": "AE", "role": "user",
        "timezone": "Europe/Paris", "createdOn": "x", "modifiedOn": "y"}],
        "pagination": _PAGE})
    (u,) = _appeler(banc, "modjo_workspace", op="users")["data"]
    assert u == {"id": 2, "email": "jane@company.example", "firstName": "Jane",
                 "lastName": "Doe", "jobTitle": "AE", "role": "user"}


def test_une_revue_garde_question_note_et_commentaire(banc):
    revue = {"id": 1, "callId": 9, "template": {"id": 3, "title": "Découverte"},
             "revieweeId": 2, "rating": 3.5, "isPrivate": False, "isAiGenerated": False,
             "createdById": 5, "createdOn": "2026-10-01T00:00:00Z", "modifiedOn": "z",
             "answers": [{"id": 11, "weight": 0, "rating": 4, "feedback": "Bien",
                          "question": {"id": 21, "title": "Besoin qualifié ?"}}]}
    banc.prepare = _rendre("list_call_reviews", {"data": [revue], "pagination": _PAGE})
    (r,) = _appeler(banc, "modjo_reviews")["data"]
    assert r["template"] == "Découverte" and "modifiedOn" not in r
    assert r["answers"] == [{"question": "Besoin qualifié ?", "rating": 4,
                             "feedback": "Bien"}]
    assert _appeler(banc, "modjo_reviews", full=True)["data"] == [revue]


def test_la_question_a_l_ia(banc):
    _appeler(banc, "modjo_ask", scope_type="deal", scope_id="42", question="Budget?",
             answer_schema={"type": "object", "properties": {"b": {"type": "boolean"}}})
    nom, args, kw = _appels(banc)[0]
    assert nom == "ask_agent" and args == ("deal", 42) and kw["instructions"] == "Budget?"
    assert "numeric id" in _refus(banc, "modjo_ask", scope_type="account", scope_id="abc",
                                  answer_schema={"type": "object"})


def test_seuls_les_outils_qui_lisent_se_declarent_en_lecture(banc):
    outils = {t.name: t for t in asyncio.run(banc.mcp.list_tools())}
    lecture = {n for n, t in outils.items() if getattr(t.annotations, "readOnlyHint", None)}
    assert lecture == {"modjo_reviews", "modjo_crm", "modjo_workspace"}


# --- refus amont ------------------------------------------------------------------

def _erreur(status, code):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"code": code, "message": "m"}, service="modjo")


@pytest.mark.parametrize("status,code,attendu", [
    (401, "invalid_api_key", "Public API"),
    (403, "feature_not_enabled", "not enabled"),
    (404, "not_found", "no call with this id"),
    (409, "not_ready", "5 to 30 minutes"),
    (410, "gone", "retention"),
])
def test_un_refus_4xx_est_nomme(banc, status, code, attendu):
    def prepare(c):
        c.leve = _erreur(status, code)
    banc.prepare = prepare
    with pytest.raises(Exception) as e:
        _appeler(banc, "modjo_calls", op="get", call_id="7")
    cause = e.value
    while cause is not None and not isinstance(cause, McpError):
        cause = cause.__cause__ or cause.__context__
    assert isinstance(cause, McpError) and attendu in str(cause)


@pytest.mark.parametrize("status", [429, 503])
def test_429_et_5xx_restent_types(banc, status):
    from oto.tools.common.errors import UpstreamHTTPError

    def prepare(c):
        c.leve = _erreur(status, "rate_limited")
    banc.prepare = prepare
    with pytest.raises(Exception) as e:
        _appeler(banc, "modjo_calls")
    cause = e.value
    while cause is not None and not isinstance(cause, UpstreamHTTPError):
        cause = cause.__cause__ or cause.__context__
    assert isinstance(cause, UpstreamHTTPError) and cause.status_code == status
