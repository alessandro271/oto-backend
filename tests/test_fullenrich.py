"""FullEnrich — métrage par unité (facturation du partenaire, 21/08): `fullenrich_enrich_linkedin`
must trace the number of contacts SUBMITTED (not enriched/found — that count only
exists later, inside `fullenrich_result`, a separate call/journal row) via
`session_org.note_call_trace(quantity=…)`, regardless of platform vs BYO key —
unlike `units` (L8, `access/reglement.py`), which only debits on the platform key and
serves a different purpose (oto's own internal quota, not org billing).

`fullenrich_result` traces what FullEnrich DEDUCTED for the job (`cost_credits`,
its `cost.credits`) once FINISHED, 0 while not finished, and nothing when no cost
is declared — no price table, no dedupe in the backend (2026-09-11)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp.tools.fullenrich import _solde as _SOLDE_REEL


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import fullenrich

    m = FastMCP("t")
    fullenrich.register(m)
    return asyncio.run(m.get_tool(name))


def _contacts(n: int) -> list[dict]:
    return [{"first_name": f"F{i}", "last_name": f"L{i}", "linkedin_slug": f"f{i}-l{i}"}
           for i in range(n)]


@pytest.fixture(autouse=True)
def _compte_approvisionne(monkeypatch):
    """La soumission lit le solde d'abord (`_solde`) : un compte approvisionné par
    défaut, pour que ces bancs n'appellent jamais FullEnrich pour de vrai."""
    from oto_mcp.tools import fullenrich
    monkeypatch.setattr(fullenrich, "_solde", lambda headers: 100)


@pytest.fixture(autouse=True)
def releves(monkeypatch):
    """Le budget d'unités doublé : ce que la relève règle ou libère, sans base."""
    from oto_mcp.access import credits as budget
    vus = {"regler": [], "liberer": []}
    monkeypatch.setattr(budget, "regler_releve",
                        lambda f, a, r: vus["regler"].append((f, a, r)) or True)
    monkeypatch.setattr(budget, "liberer_releve",
                        lambda f, a: vus["liberer"].append((f, a)) or True)
    return vus


def test_un_compte_A_SEC_est_refuse_AVANT_de_soumettre(monkeypatch):
    """Signaux oto #1276, #1280, #1340 : FullEnrich acceptait un lot qu'il ne pouvait
    pas payer, et l'échec n'apparaissait qu'au relevé. Le refus est un 402, que la
    taxonomie lit `quota_exhausted` (et qui marque la clé servie)."""
    from oto_mcp import error_taxonomy
    from oto_mcp.tools import fullenrich
    monkeypatch.setattr(fullenrich, "_solde", lambda headers: 0)
    with patch("oto_mcp.access.resolve_api_key", return_value=("k", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        with pytest.raises(fullenrich.FullenrichASec) as exc:
            _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(3))
    client_cls.return_value.submit.assert_not_called()
    trace.assert_not_called()
    assert error_taxonomy.classify(exc.value).code == "quota_exhausted"


@pytest.mark.parametrize("solde", [5404.75, "5404.75", 0.5])
def test_un_solde_DECIMAL_laisse_soumettre(monkeypatch, solde):
    """Signal #1442 (14/10/2026) : un solde décimal était lu « illisible » et chaque
    soumission échouait avant l'envoi. Passe par la VRAIE lecture du solde, que la
    fixture remplace partout ailleurs."""
    import requests
    from oto_mcp.tools import fullenrich
    monkeypatch.setattr(fullenrich, "_solde", _SOLDE_REEL)
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"balance": solde}))
    with patch("oto_mcp.access.resolve_api_key", return_value=("k", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace"), \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_789"
        out = _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))
    client_cls.return_value.submit.assert_called_once()
    assert out["enrichment_id"] == "enr_789"


@pytest.mark.parametrize("is_platform", [True, False])
def test_enrich_linkedin_traces_the_submitted_contact_count(is_platform):
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", is_platform)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_123"
        _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(7))

    # Unconditional — fires the SAME on a platform key and a BYO key, unlike
    # `units` (settled on the platform key only, a different mechanism/purpose).
    trace.assert_any_call(quantity=7)


def test_enrich_linkedin_traces_the_batch_size_not_a_fixed_value():
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_456"
        _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(100))

    trace.assert_called_once_with(quantity=100)


def test_enrich_linkedin_does_not_trace_on_a_rejected_submission():
    """A submit() failure (ValueError → McpError) must not leave a stale trace —
    nothing was actually billed against oto's own credits, so nothing should be
    billed against the org's either."""
    from oto_mcp.mcp_errors import McpError

    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.side_effect = ValueError("bad contact")
        with pytest.raises(McpError):
            _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))

    trace.assert_not_called()


# ── `fullenrich_result` : le coût DÉCLARÉ par FullEnrich, relevé tel quel ─────
#
# Le client oto-core rend `cost_credits` (le `cost.credits` du job). Les tests
# passent par `res.get(...)` sur un client simulé : ils tiennent sur le pin actuel,
# que le champ y soit déjà ou non.

def _result(fetched: dict, *, is_platform: bool = True, **kw):
    rc = SimpleNamespace(key="fake-key", is_platform=is_platform)
    with patch("oto_mcp.access.resolve_credential", return_value=rc), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace") as trace, \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.fetch.return_value = fetched
        out = _tool("fullenrich_result").fn(enrichment_id="enr_789", **kw)
    return out, trace


def _profile(*, work_emails=(), personal_emails=(), phones=()):
    """Un profil tel que le client le rend (attributs + `to_dict`), sans dépendre du pin."""
    p = SimpleNamespace(work_emails=list(work_emails), personal_emails=list(personal_emails),
                        phones=list(phones))
    p.to_dict = lambda: {"work_emails": p.work_emails, "personal_emails": p.personal_emails,
                         "phones": p.phones}
    return p


_RIEN = {"found_work_emails": 0, "found_personal_emails": 0, "found_phones": 0}


@pytest.mark.parametrize("is_platform", [True, False])
def test_result_finished_traces_the_credits_fullenrich_deducted(is_platform):
    out, trace = _result({"status": "FINISHED", "profiles": [], "cost_credits": 14},
                         is_platform=is_platform)
    assert out["done"] is True
    # INCONDITIONNEL : même trace sur la clé plateforme et sur une clé BYO — le
    # consommateur filtre sur `key_mode`, pas le backend.
    trace.assert_called_once_with(quantity=14, **_RIEN)


def test_result_finished_with_zero_credits_traces_a_zero():
    """Rien trouvé = rien déduit chez FullEnrich : un zéro mesuré, pas une absence."""
    _, trace = _result({"status": "FINISHED", "profiles": [], "cost_credits": 0})
    trace.assert_called_once_with(quantity=0, **_RIEN)


def test_result_finished_counts_CONTACTS_per_kind_not_values():
    """Deux e-mails pro sur un contact comptent UNE fois ; un contact sans rien ne
    compte nulle part ; chaque sorte se compte à part."""
    profiles = [
        _profile(work_emails=["a@x.fr", "a.b@x.fr"], phones=["+33600000001"]),
        _profile(work_emails=["c@y.fr"], personal_emails=["c@gmail.com", "c2@gmail.com"]),
        _profile(),
        _profile(phones=["+33600000002", "+33600000003"]),
    ]
    _, trace = _result({"status": "FINISHED", "profiles": profiles, "cost_credits": 25})
    trace.assert_called_once_with(quantity=25, found_work_emails=2,
                                  found_personal_emails=1, found_phones=2)


@pytest.mark.parametrize("status", ["CREATED", "IN_PROGRESS"])
def test_result_not_finished_traces_zero_and_no_counts(status):
    out, trace = _result({"status": status, "profiles": None, "cost_credits": None})
    assert out["done"] is False
    trace.assert_called_once_with(quantity=0)


@pytest.mark.parametrize("fetched", [
    {"status": "FINISHED", "profiles": [_profile(work_emails=["a@x.fr"])]},     # oto-core antérieur au champ
    {"status": "FINISHED", "profiles": [_profile(work_emails=["a@x.fr"])],
     "cost_credits": None},                                                       # amont muet
])
def test_result_finished_without_a_declared_cost_traces_counts_but_no_quantity(fetched):
    """Pas de repli calculé depuis les profils : un barème (1/3/10) n'a pas sa place
    dans le backend. Sans coût déclaré, aucune quantité — jamais une valeur devinée.
    Les comptes, eux, sont des faits lus dans les profils : ils partent quand même."""
    out, trace = _result(fetched)
    assert out["done"] is True
    trace.assert_called_once_with(found_work_emails=1, found_personal_emails=0, found_phones=0)


def test_the_found_counts_reach_the_journal_and_bill_only_on_the_target_row():
    """Les trois noms sont dans la liste fermée des args journalisés, et parmi les clés
    qui facturent (gardées sur la ligne cible d'un `oto_call`, jamais sur l'enveloppe) ;
    la lentille de facturation les lit sous ces mêmes noms."""
    from oto_mcp import server
    from oto_mcp.calllog import apply_call_trace
    from oto_mcp.db import usage as dbu
    from oto_mcp.tools import meta

    noms = set(dbu.BILLABLE_FOUND_ARGS.values())
    assert noms <= set(server._TRACED_ARGS)
    assert noms <= set(meta._BILLING_TRACE_KEYS)
    row = apply_call_trace({"args": {"enrichment_id": "enr_789"}},
                           {"quantity": 25, "found_work_emails": 2,
                            "found_personal_emails": 1, "found_phones": 2},
                           server._TRACED_ARGS)
    assert row["args"] == {"enrichment_id": "enr_789", "found_work_emails": 2,
                           "found_personal_emails": 1, "found_phones": 2}
    assert row["quantity"] == 25


# ── Relever un job n'est jamais une boucle sans fin (signaux #943, #990, #1027-#1029) ──

def _en_cours(status="IN_PROGRESS"):
    return {"status": status, "profiles": None, "cost_credits": None}


def test_le_releve_ne_verifie_pas_le_quota_plateforme():
    """#943 : le quota est débité à la SOUMISSION ; le relevé ne consomme rien. Le
    vérifier rendait le résultat d'un job déjà payé illisible le jour même."""
    rc = SimpleNamespace(key="fake-key", is_platform=True)
    with patch("oto_mcp.access.resolve_credential", return_value=rc) as resolve, \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace"), \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.fetch.return_value = _en_cours()
        _tool("fullenrich_result").fn(enrichment_id="enr_789")
    resolve.assert_called_once_with("fullenrich", check_usage=False)


def test_un_job_en_cours_dit_quand_repasser():
    out, _ = _result(_en_cours())
    assert out["done"] is False and out["retry_after_s"] == 30


def test_une_limite_de_debit_dit_d_attendre_plus():
    out, _ = _result(_en_cours("RATE_LIMIT"))
    assert out["done"] is False and out["retry_after_s"] == 60


@pytest.mark.parametrize("status,code", [
    ("CANCELED", "fullenrich_job_canceled"),
    ("NOT_FOUND", "fullenrich_job_not_found"),
    ("UNKNOWN", "fullenrich_job_status_unknown"),
    ("ÉTRANGE", "fullenrich_job_status_unknown"),
])
def test_un_statut_terminal_est_un_refus_nomme_pas_un_en_cours(status, code):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as e:
        _result(_en_cours(status))
    assert e.value.error.data["code"] == code
    assert e.value.error.data["retryable"] is False
    assert code in e.value.error.message


@pytest.mark.parametrize("erreur, attendu", [
    # Toute réponse non 2xx que le client ne lit pas comme un statut.
    (UpstreamHTTPError(502, "bad gateway", service="fullenrich", method="GET",
                       url="https://app.fullenrich.com/api/v2/contact/enrich/bulk/enr_789"),
     "502"),
    # Un 200 dont le corps dit `CREDITS_INSUFFICIENT` : le client lève un RuntimeError.
    (RuntimeError("FullEnrich: insufficient credits. Top up at app.fullenrich.com."),
     "insufficient credits"),
])
def test_une_erreur_de_l_amont_est_nommee_pas_interne(erreur, attendu):
    from oto_mcp.mcp_errors import McpError
    rc = SimpleNamespace(key="fake-key", is_platform=False)
    with patch("oto_mcp.access.resolve_credential", return_value=rc), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace"), \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.fetch.side_effect = erreur
        with pytest.raises(McpError) as e:
            _tool("fullenrich_result").fn(enrichment_id="enr_789")
    assert e.value.error.data["code"] == "fullenrich_upstream_error"
    assert attendu in e.value.error.message


def test_la_soumission_rend_son_heure():
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.fullenrich.session_org.note_call_trace"), \
         patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
        client_cls.return_value.submit.return_value = "enr_1"
        out = _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))
    import datetime as dt
    assert dt.datetime.fromisoformat(out["submitted_at"]).tzinfo is not None


def test_passe_le_plafond_le_releve_dit_d_arreter():
    import datetime as dt
    vieux = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25)).isoformat()
    out, _ = _result(_en_cours(), submitted_at=vieux)
    assert out["done"] is False
    assert out["verdict"] == "still_running_after_20_min"
    assert "ne resoumets pas" in out["next_step"].lower() or "do not resubmit" in out["next_step"].lower()


def test_sous_le_plafond_pas_de_verdict():
    import datetime as dt
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat()
    out, _ = _result(_en_cours(), submitted_at=recent)
    assert "verdict" not in out


def test_une_heure_de_soumission_illisible_est_refusee():
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError, match="submitted_at"):
        _result(_en_cours(), submitted_at="hier")


# ── le budget d'unités (`access/credits.py`) ─────────────────────────────────

def test_la_soumission_est_au_perimetre_avec_son_pire_cas():
    """Les poids de FullEnrich par champ demandé (`fullenrich.submit` dans la
    bibliothèque des connecteurs), fois les contacts ; les champs par défaut du client
    quand l'agent n'en demande pas."""
    from oto.connectors import cost
    from oto_mcp.access import credits as budget
    _tool("fullenrich_enrich_linkedin")
    c = budget.perimetre()["fullenrich_enrich_linkedin"]
    assert (c.connector, c.function) == ("fullenrich", "submit")
    pire = cost.worst_case(c.connector, c.function, c.arguments({"contacts": _contacts(2)}))
    assert pire == {"quantity": 22, "unit": "credits"}, "2 × (1 e-mail pro + 10 téléphone)"
    pire = cost.worst_case(c.connector, c.function, c.arguments(
        {"contacts": _contacts(3), "enrich_fields": ["contact.personal_emails"]}))
    assert pire["quantity"] == 9


def test_la_soumission_dit_son_envoi_et_sa_reponse(monkeypatch):
    from oto_mcp import session_org
    from oto_mcp.access import credits as budget
    _tool("fullenrich_enrich_linkedin")   # au périmètre ; le client se lie à l'enregistrement
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        budget.ouvrir_appel("fullenrich_enrich_linkedin", {"contacts": _contacts(1)})
        with patch("oto_mcp.access.resolve_api_key", return_value=("k", True)), \
             patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
            client_cls.return_value.submit.return_value = "enr_1"
            _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))
        appel = releve[budget.CLE]
        assert appel["envoye"] is True and "net" not in appel
        assert appel["reponses"] == [{"enrichment_id": "enr_1"}]
        budget.ouvrir_appel("fullenrich_enrich_linkedin", {"contacts": _contacts(1)})
        with patch("oto_mcp.access.resolve_api_key", return_value=("k", True)), \
             patch("oto.tools.fullenrich.client.FullenrichClient") as client_cls:
            client_cls.return_value.submit.side_effect = ValueError("domain requis")
            with pytest.raises(Exception):
                _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))
        assert releve[budget.CLE]["net"] is True, "refusée par le client avant l'envoi"
    finally:
        session_org.reset_call_trace(jeton)


@pytest.mark.parametrize("is_platform", [True, False])
def test_la_releve_finie_regle_la_reservation_a_ce_que_fullenrich_a_deduit(releves,
                                                                           is_platform):
    _result({"status": "FINISHED", "profiles": [], "cost_credits": 14},
            is_platform=is_platform)
    attendu = [("fullenrich.fetch", {"enrichment_id": "enr_789"},
                [{"cost": {"credits": 14}}])]
    assert releves["regler"] == (attendu if is_platform else [])


def test_une_releve_sans_cout_declare_regle_au_montant_reserve(releves):
    _result({"status": "FINISHED", "profiles": [], "cost_credits": None})
    assert releves["regler"] == [("fullenrich.fetch", {"enrichment_id": "enr_789"}, [])]


def test_un_travail_annule_libere_sa_reservation_les_autres_attendent(releves):
    with pytest.raises(Exception):
        _result({"status": "CANCELED", "profiles": None, "cost_credits": None})
    with pytest.raises(Exception):
        _result({"status": "NOT_FOUND", "profiles": None, "cost_credits": None})
    _result({"status": "IN_PROGRESS", "profiles": None, "cost_credits": None})
    assert releves["liberer"] == [("fullenrich.fetch", {"enrichment_id": "enr_789"})]
    assert releves["regler"] == []


@pytest.mark.parametrize("statut, solde", [(400, "liberer"), (422, "liberer"),
                                            (429, "regler"), (500, "regler")])
def test_un_post_refuse_par_fullenrich_libere_sa_reservation(monkeypatch, statut, solde):
    """Un POST que l'API FullEnrich refuse (4xx autre que 408 et 429) n'a rien consommé :
    le client d'oto-core lève `UpstreamHTTPError` avec son statut, le refus net le lit,
    et l'échec LIBÈRE la réservation. Un 429 (rejouable) ou un 5xx se règle au montant
    réservé. Le vrai client : seul le transport HTTP est doublé."""
    import requests
    from oto_mcp import session_org
    from oto_mcp.access import credits as budget
    from oto_mcp.db import credits as db_credits
    from oto_mcp.tools import fullenrich
    monkeypatch.setattr(fullenrich, "_solde", lambda headers: 10_000)
    _tool("fullenrich_enrich_linkedin")   # au périmètre ; le client se lie à l'enregistrement
    reponse = requests.Response()
    reponse.status_code = statut
    reponse._content = b'{"code": "error.enrichment.refused", "message": "refused"}'
    reponse.headers["Content-Type"] = "application/json"
    monkeypatch.setattr("oto.tools.fullenrich.client.requests.post",
                        lambda *a, **k: reponse)
    vus = []
    monkeypatch.setattr(db_credits, "liberer", lambda uid: vus.append("liberer"))
    monkeypatch.setattr(db_credits, "regler",
                        lambda uid, **k: vus.append("regler") or {"ok": 1})
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        budget.ouvrir_appel("fullenrich_enrich_linkedin", {"contacts": _contacts(1)})
        with patch("oto_mcp.access.resolve_api_key", return_value=("k", True)):
            with pytest.raises(Exception) as e:
                _tool("fullenrich_enrich_linkedin").fn(contacts=_contacts(1))
    finally:
        session_org.reset_call_trace(jeton)
    assert getattr(e.value, "status_code", None) == statut, "l'erreur du vrai client"
    releve[budget.CLE]["hold"] = {"reserved": 11, "quantity": 11, "payer": "org:1"}
    budget.solder_echec(releve, e.value)
    assert vus == [solde]
