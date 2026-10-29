"""Les tables d'étapes email, téléphone et personne (`waterfall/email.py`,
`telephone.py`, `personne.py`) — ce que chaque fournisseur reçoit et comment sa réponse
est lue ; les étapes réservées à la clé de l'appelant sont sautées AVANT l'appel sur la
clé partagée."""
import pytest

from oto_mcp.tools.meta import IssueCible
from oto_mcp.waterfall import email, personne

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs


# --- lemlist : le profil LinkedIn ---------------------------------------------------

def test_lemlist_personne_lit_le_profil_linkedin(calls):
    calls["answers"]["lemlist_enrich"] = IssueCible(ok=True, result={"enrichment_id": "enr_3"})
    calls["answers"]["lemlist_enrich_result"] = IssueCible(ok=True, result={"results": [{
        "done": True, "found": {"linkedin": True},
        "data": {"linkedin": {"firstName": "Jane", "jobTitle": "CTO", "companyName": "Acme",
                              "locationName": "Paris"}}}]})
    out = _run("waterfall_person", linkedin_url="https://linkedin.com/in/jane", providers=["lemlist"])
    assert out["person"]["title"] == "CTO" and out["person"]["company_name"] == "Acme"


# --- personne ---------------------------------------------------------------------

def test_personne_aiark_puis_apollo(calls):
    calls["answers"]["linkedin_unipile_profile"] = IssueCible(ok=False, code="invalid_input",
                                                              message="no LinkedIn account")
    calls["answers"]["linkedin_aiark_person"] = IssueCible(ok=True, result={"found": False})
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {
        "title": "Head of Sales", "seniority": "head", "city": "Lyon",
        "organization": {"name": "Acme", "primary_domain": "acme.com"}}})
    out = _run("waterfall_person", linkedin_url="https://linkedin.com/in/jane")
    assert out["provider"] == "apollo"
    assert out["person"] == {"title": "Head of Sales", "company_name": "Acme",
                             "company_domain": "acme.com", "seniority": "head", "city": "Lyon",
                             "linkedin_url": "https://linkedin.com/in/jane"}
    assert calls["made"][0] == ("linkedin_unipile_profile", {"op": "person", "identifier": "jane"})


def test_personne_sans_titre_ni_societe_n_est_pas_un_trouve(calls):
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {"name": "Jane"}})
    out = _run("waterfall_person", email="jane@acme.com", providers=["apollo"])
    assert out["found"] is False
    assert calls["made"][0][1]["email"] == "jane@acme.com"


# --- les asynchrones ajoutés : FullEnrich, Dropcontact, révélation Apollo ----------

def test_fullenrich_telephone_soumet_un_slug_puis_releve(calls):
    calls["answers"]["fullenrich_enrich_linkedin"] = IssueCible(ok=True, result={
        "enrichment_id": "fe_1", "submitted_at": "2026-10-19T10:00:00Z"})
    calls["answers"]["fullenrich_result"] = [
        IssueCible(ok=True, result={"done": False, "retry_after_s": 20}),
        IssueCible(ok=True, result={"done": True, "profiles": [
            {"found": True, "phones": ["+33 6 11 22 33 44"], "work_emails": []}]}),
    ]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/Jane-Doe/",
               full_name="Jane Doe", providers=["fullenrich"])
    assert out["provider"] == "fullenrich" and out["phone"]["phone"] == "+33 6 11 22 33 44"
    assert calls["made"][0] == ("fullenrich_enrich_linkedin", {
        "contacts": [{"first_name": "Jane", "last_name": "Doe", "linkedin_slug": "jane-doe"}],
        "enrich_fields": ["contact.phones"]})
    assert calls["made"][1][1] == {"enrichment_id": "fe_1", "submitted_at": "2026-10-19T10:00:00Z"}


def test_dropcontact_email_ecarte_une_adresse_qu_il_dit_invalide(calls):
    calls["answers"]["dropcontact_enrich"] = IssueCible(ok=True, result={"request_id": "dc_1"})
    calls["answers"]["dropcontact_result"] = IssueCible(ok=True, result={"done": True, "profiles": [
        {"email": [{"email": "old@acme.com", "qualification": "invalid@invalid"},
                   {"email": "jane@acme.com", "qualification": "nominative@pro"}]}]})
    out = _run("waterfall_email", full_name="Jane Doe", domain="acme.com",
               providers=["dropcontact"])
    assert out["email"] == {"email": "jane@acme.com", "all": ["jane@acme.com"],
                            "status": "nominative@pro"}
    assert calls["made"][0][1] == {"contacts": [
        {"first_name": "Jane", "last_name": "Doe", "full_name": "Jane Doe", "website": "acme.com"}]}


def test_revelation_apollo_mobile_d_abord_et_sans_correspondance_rien_n_est_releve(calls):
    calls["modes"] = {"apollo_reveal_phone": "org"}    # own key only
    calls["answers"]["apollo_reveal_phone"] = IssueCible(ok=True, result={"request_id": 123})
    calls["answers"]["apollo_reveal_phone_result"] = IssueCible(ok=True, result={
        "done": True, "result": {"webhook_result": {"people": [{"phone_numbers": [
            {"sanitized_number": "+14155550100", "type_cd": "work_direct"},
            {"sanitized_number": "+14155550199", "type_cd": "mobile"}]}]}}})
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo"])
    assert out["phone"]["phone"] == "+14155550199"
    assert calls["made"][1] == ("apollo_reveal_phone_result", {"request_id": "123"})

    calls["made"].clear()
    calls["answers"]["apollo_reveal_phone"] = IssueCible(ok=True, result={"matched": False})
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo"])
    assert out["attempts"][0]["status"] == "no_match"
    assert [n for n, _ in calls["made"]] == ["apollo_reveal_phone"]


def test_revelation_apollo_non_livree_n_est_pas_un_trouve(calls):
    calls["modes"] = {"apollo_reveal_phone": "org"}    # own key only
    calls["answers"]["apollo_reveal_phone"] = IssueCible(ok=True, result={"request_id": "9"})
    calls["answers"]["apollo_reveal_phone_result"] = IssueCible(ok=True, result={
        "done": True, "delivered": False, "reason": "out_of_credits"})
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo"])
    assert out["found"] is False


def test_la_revelation_de_telephone_apollo_exige_la_cle_de_l_appelant(calls):
    calls["modes"] = {"apollo_reveal_phone": "platform"}
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo", "lusha"])
    assert out["attempts"][0]["code"] == "own_key_required"
    assert "apollo_reveal_phone" not in _made(calls)
    calls["modes"] = {"apollo_reveal_phone": "org"}
    calls["made"].clear()
    _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo"])
    assert _made(calls)[0] == "apollo_reveal_phone"



# --- réponses RÉELLES (essai sur des exposants d'un salon, 2026-10-19), anonymisées --------

def test_reel_aiark_export_statut_en_capitales(calls):
    calls["answers"]["linkedin_aiark_person"] = IssueCible(ok=True, result={
        "found": True, "profile": {"full_name": "Jane Doe", "title": "CEO"},
        "email": {"state": "DONE", "output": [
            {"address": "j.d@other.example", "status": "INVALID"},
            {"address": "jane@acme.com", "status": "VALID", "found": True}]}})
    out = _run("waterfall_email", linkedin_url="https://www.linkedin.com/in/jane-doe-1",
               providers=["aiark"])
    assert out["email"]["email"] == "jane@acme.com" and out["email"]["status"] == "VALID"


def test_reel_apollo_sans_email_ne_compte_pas_mais_la_personne_si(calls):
    person = {"person": {"name": "Sam Roe", "title": "Sales Leader", "seniority": "manager",
                         "linkedin_url": "http://www.linkedin.com/in/sam-ro%c3%a7-1",
                         "email": None, "email_status": "unavailable",
                         "organization": {"name": "ACME", "primary_domain": "acme.com"}}}
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result=person)
    url = "https://www.linkedin.com/in/sam-roç-1"
    assert _run("waterfall_email", linkedin_url=url, providers=["apollo"])["found"] is False
    out = _run("waterfall_person", linkedin_url=url, providers=["apollo"])
    assert out["person"]["title"] == "Sales Leader"
    assert out["person"]["linkedin_url"] == url        # the caller's, not Apollo's %-encoded one


def test_reel_linkedin_non_connecte_est_saute(calls):
    calls["answers"]["linkedin_unipile_profile"] = IssueCible(
        ok=False, code="invalid_input",
        message="Connect your Linkedin account at https://example.test/console/connections "
                "(or via `unipile_connect_start`) before using these tools.")
    out = _run("waterfall_person", linkedin_url="https://linkedin.com/in/jane", providers=["linkedin"])
    assert out["attempts"][0]["status"] == "skipped" and out["attempts"][0]["code"] == "not_connected"


# --- Forager : la clé partagée ne sert que le téléphone ----------------------------

def test_forager_email_et_personne_exigent_la_cle_de_l_appelant(calls):
    """La clé partagée de Forager ne sert qu'une recherche — les numéros d'une personne
    (`tools/forager.py::PLATFORM_OPS`) : l'email et le profil sont sautés AVANT l'appel
    sur toute autre clé que celle de l'appelant."""
    for steps in (email.EMAIL_STEPS, personne.PERSON_STEPS):
        assert next(s for s in steps if s.key == "forager").own_key_only
    calls["modes"] = {"forager_person": "platform"}
    for ns in ("waterfall_email", "waterfall_person"):
        out = _run(ns, linkedin_url="https://linkedin.com/in/jane", providers=["forager"])
        assert out["attempts"][0]["code"] == "own_key_required"
    assert "forager_person" not in _made(calls)
    calls["modes"] = {"forager_person": "org"}
    _run("waterfall_email", linkedin_url="https://linkedin.com/in/jane", providers=["forager"])
    assert _made(calls) == ["forager_person"]


def test_forager_email_part_sur_la_cle_partagee_quand_l_instance_l_ouvre(calls, monkeypatch):
    """L'instance ouvre `forager_person:work_emails` sur sa clé partagée (`platform.ops`) :
    l'étape email n'est plus « clé de l'appelant seulement » pour cet appel — le plan le
    dit comme l'appel. Le profil (`detail`), non ouvert, reste sauté."""
    from oto_mcp.waterfall import ordre, plan
    calls["forager_ops"] = "forager_person:phone_numbers,forager_person:work_emails"
    calls["modes"] = {"forager_person": "platform"}
    _run("waterfall_email", linkedin_url="https://linkedin.com/in/jane", providers=["forager"])
    assert _made(calls) == ["forager_person"]
    out = _run("waterfall_person", linkedin_url="https://linkedin.com/in/jane",
               providers=["forager"])
    assert out["attempts"][0]["code"] == "own_key_required"
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: ["forager"])
    (row,) = plan.plan("waterfall_email", "sub-test")["steps"]
    assert row["usable"] is True and "own_key_only" not in row


def test_un_reglage_forager_illisible_garde_l_etape_sur_la_cle_de_l_appelant(calls, monkeypatch):
    from oto_mcp.tools import forager

    def illisible():
        raise ConnectionError("la base ne répond pas")
    monkeypatch.setattr(forager, "platform_ops", illisible)
    calls["modes"] = {"forager_person": "platform"}
    out = _run("waterfall_email", linkedin_url="https://linkedin.com/in/jane",
               providers=["forager"])
    assert out["attempts"][0]["code"] == "own_key_required"
    assert "forager_person" not in _made(calls)


# --- pipecorn, en tête du téléphone (asynchrone) ---------------------------------------

PID = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
SOUMIS = IssueCible(ok=True, result={"enrichment_id": PID, "status": "pending",
                                     "find": ["phone"]})


@pytest.fixture
def horloge(monkeypatch):
    """Une horloge virtuelle : l'attente de Pipecorn (20 s au plus) passe sans attendre."""
    import types
    from oto_mcp.waterfall import appel, moteur
    t = {"now": 1000.0}

    async def dormir(s):
        t["now"] += s
    faux = types.SimpleNamespace(monotonic=lambda: t["now"])
    monkeypatch.setattr(appel, "_sleep", dormir)
    monkeypatch.setattr(appel, "time", faux)
    monkeypatch.setattr(moteur, "time", faux)
    return t


def _releve(details):
    return IssueCible(ok=True, result={
        "done": True, "status": "finished", "phone": [d["number"] for d in details],
        "phone_details": details, "pipecorn_credits": 30 if details else 0})


def test_pipecorn_ouvre_le_telephone_soumet_un_mobile_puis_releve(calls, horloge):
    calls["answers"]["pipecorn"] = [
        SOUMIS,
        IssueCible(ok=True, result={"done": False, "status": "pending"}),
        _releve([{"number": "+33612345678", "type": "mobile"}]),
    ]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/")
    assert out["found"] is True and out["provider"] == "pipecorn"
    assert out["phone"] == {"phone": "+33612345678", "all": ["+33612345678"]}
    assert calls["made"][0] == ("pipecorn", {
        "op": "enrich", "find": ["phone"],
        "linkedin_url": "https://www.linkedin.com/in/jane-doe/", "phone_type": "mobile"})
    relevé = {"op": "result", "enrichment_id": PID, "find": ["phone"]}
    assert calls["made"][1:] == [("pipecorn", relevé), ("pipecorn", relevé)]


def test_pipecorn_sans_mobile_passe_la_main(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS, _releve([])]
    calls["answers"]["kaspr_enrich_linkedin"] = IssueCible(
        ok=True, result={"profile": {"phone": "+33 6 11 22 33 44"}})
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/")
    assert out["provider"] == "kaspr"
    assert _made(calls)[:3] == ["pipecorn", "pipecorn", "kaspr_enrich_linkedin"]


def test_pipecorn_attend_20_s_au_plus_puis_passe_la_main(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS, IssueCible(ok=True, result={"done": False})]
    calls["answers"]["kaspr_enrich_linkedin"] = IssueCible(
        ok=True, result={"profile": {"phone": "+33 6 11 22 33 44"}})
    debut = horloge["now"]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/")
    assert out["provider"] == "kaspr" and out["found"] is True
    lectures = [a for n, a in calls["made"] if n == "pipecorn" and a.get("op") == "result"]
    # 2 s, puis toutes les 3 s jusqu'à 20 s ; puis UNE relecture en fin de cascade.
    assert len(lectures) == 1 + 6 + 1
    assert 19 <= horloge["now"] - debut <= 21
    by = {a["provider"]: a for a in out["attempts"]}
    assert by["pipecorn"]["status"] == "pending"
    assert out["pending"] == [{"provider": "pipecorn", "tool": "pipecorn", "arguments": {
        "op": "result", "enrichment_id": PID, "find": ["phone"]}}]


def test_l_attente_de_pipecorn_compte_dans_le_budget_de_la_cascade(calls, horloge,
                                                                  monkeypatch):
    # Un budget de 8 s : Pipecorn est relu à 2, 5 et 8 s, puis plus rien ne part — ni
    # Kaspr ni la relecture finale. La recherche reste `pending`, à relever plus tard :
    # l'appel tient sous la borne REST, aucun appel n'est coupé après dépense.
    from oto_mcp.waterfall import moteur
    monkeypatch.setattr(moteur, "DEADLINE_S", 8.0)
    calls["answers"]["pipecorn"] = [SOUMIS, IssueCible(ok=True, result={"done": False})]
    debut = horloge["now"]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/")
    lectures = [a for n, a in calls["made"] if n == "pipecorn" and a.get("op") == "result"]
    assert len(lectures) == 3
    assert horloge["now"] - debut == 8
    assert "kaspr_enrich_linkedin" not in _made(calls)
    by = {a["provider"]: a for a in out["attempts"]}
    assert by["kaspr"]["code"] == "time_budget"
    assert out["found"] is False
    assert out["pending"] == [{"provider": "pipecorn", "tool": "pipecorn", "arguments": {
        "op": "result", "enrichment_id": PID, "find": ["phone"]}}]


def test_apres_l_attente_de_pipecorn_les_suivants_ont_ce_qui_reste(calls, horloge):
    # 20 s pour Pipecorn sur 40 : Kaspr ne trouve rien, Lusha passe encore.
    calls["answers"]["pipecorn"] = [SOUMIS, IssueCible(ok=True, result={"done": False})]
    calls["answers"]["lusha_search_and_enrich"] = IssueCible(ok=True, result={
        "results": [{"phones": ["+33612345678"]}]})
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/")
    assert out["provider"] == "lusha"


def test_pipecorn_fini_apres_le_repli_est_relu_et_sert_si_rien_n_a_trouve(calls, horloge):
    # Rien en 20 s ; les suivants ne trouvent rien ; la relecture finale a le mobile.
    calls["answers"]["pipecorn"] = [SOUMIS] + [
        IssueCible(ok=True, result={"done": False})] * 7 + [
        _releve([{"number": "+33612345678", "type": "mobile"}])]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               providers=["pipecorn", "kaspr"])
    assert out["provider"] == "pipecorn" and out["phone"]["phone"] == "+33612345678"
    assert "pending" not in out
    assert out["attempts"][0]["status"] == "found"


def test_pipecorn_fini_apres_un_autre_est_note_sans_remplacer(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS] + [
        IssueCible(ok=True, result={"done": False})] * 7 + [
        _releve([{"number": "+33600000000", "type": "mobile"}])]
    calls["answers"]["kaspr_enrich_linkedin"] = IssueCible(
        ok=True, result={"profile": {"phone": "+33 6 11 22 33 44"}})
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               providers=["pipecorn", "kaspr"])
    assert out["provider"] == "kaspr"
    assert out["attempts"][0]["status"] == "found_late"
    assert "billed" in out["attempts"][0]["message"]
    assert "pending" not in out


def test_pipecorn_un_numero_masque_n_est_pas_un_trouve(calls, horloge):
    calls["answers"]["pipecorn"] = [
        SOUMIS, _releve([{"number": "Contacts not shared", "type": "mobile"}])]
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               providers=["pipecorn"])
    assert out["found"] is False


def test_pipecorn_sans_profil_linkedin_est_saute(calls):
    out = _run("waterfall_phone", full_name="Jane Doe", domain="acme.com", dry_run=True)
    by = {a["provider"]: a for a in out["attempts"]}
    assert by["pipecorn"]["status"] == "skipped"
    assert "pipecorn" not in _made(calls)


def test_pipecorn_sur_la_cle_partagee_passe_derriere_les_cles_de_l_appelant(calls):
    calls["modes"] = {"kaspr_enrich_linkedin": "user"}
    out = _run("waterfall_phone", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               dry_run=True)
    order = [a["provider"] for a in out["attempts"]]
    assert order[:2] == ["kaspr", "pipecorn"]


# --- pipecorn ailleurs : email (asynchrone, en tête), personne, société, techno -------

def _email_releve(email):
    return IssueCible(ok=True, result={"done": True, "status": "finished", "email": email,
                                       "email_status": "deliverable",
                                       "pipecorn_credits": 3 if email else 0})


def test_pipecorn_ouvre_l_email_par_le_nom_et_le_domaine(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS, _email_releve("Jane@Acme.com")]
    out = _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com",
               providers=["pipecorn"])
    assert out["provider"] == "pipecorn"
    assert out["email"] == {"email": "jane@acme.com", "all": ["jane@acme.com"],
                            "status": "deliverable"}
    assert calls["made"][0] == ("pipecorn", {"op": "enrich", "find": ["email"],
                                             "firstname": "Jane", "lastname": "Doe",
                                             "domain": "acme.com"})
    assert calls["made"][1][1] == {"op": "result", "enrichment_id": PID, "find": ["email"]}


def test_pipecorn_email_par_le_profil_seul_quand_il_y_en_a_un(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS, _email_releve("jane@acme.com")]
    _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com",
         linkedin_url="https://www.linkedin.com/in/jane-doe/", providers=["pipecorn"])
    assert calls["made"][0][1] == {"op": "enrich", "find": ["email"],
                                   "linkedin_url": "https://www.linkedin.com/in/jane-doe/"}


def test_un_email_trouve_au_premier_fournisseur_n_attend_ni_ne_paie_pipecorn(calls, horloge):
    """Pipecorn passe APRÈS les étapes synchrones : Hunter trouve, Pipecorn n'est jamais
    soumis — ni attente, ni second paiement."""
    calls["answers"]["pipecorn"] = [SOUMIS, _email_releve("jane@acme.com")]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com"}})
    debut = horloge["now"]
    out = _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com")
    assert out["provider"] == "hunter"
    assert "pipecorn" not in _made(calls)
    assert horloge["now"] - debut < 1


def test_pipecorn_email_attend_20_s_au_plus_puis_passe_la_main_a_hunter(calls, horloge):
    calls["answers"]["pipecorn"] = [SOUMIS, IssueCible(ok=True, result={"done": False})]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com"}})
    debut = horloge["now"]
    out = _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com",
               providers=["pipecorn", "hunter"])
    assert out["provider"] == "hunter"
    assert 19 <= horloge["now"] - debut <= 21
    assert out["pending"] == [{"provider": "pipecorn", "tool": "pipecorn", "arguments": {
        "op": "result", "enrichment_id": PID, "find": ["email"]}}]


def test_pipecorn_email_fini_apres_hunter_est_dit_paye(calls, horloge):
    """Un email que Pipecorn trouve après le repli est payé tout de même : le reçu le dit."""
    calls["answers"]["pipecorn"] = [SOUMIS] + [
        IssueCible(ok=True, result={"done": False})] * 7 + [_email_releve("j.doe@acme.com")]
    calls["answers"]["hunter_email_finder"] = IssueCible(
        ok=True, result={"data": {"email": "jane@acme.com"}})
    out = _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com",
               providers=["pipecorn", "hunter"])
    assert out["provider"] == "hunter"
    assert out["attempts"][0]["status"] == "found_late"
    assert "billed" in out["attempts"][0]["message"]
    step = next(s for s in email.EMAIL_STEPS if s.key == "pipecorn")
    assert "even if another provider found first" in step.explains


def test_pipecorn_email_sert_sur_la_cle_partagee(calls, horloge):
    calls["modes"] = {"pipecorn": "platform"}
    calls["answers"]["pipecorn"] = [SOUMIS, _email_releve("jane@acme.com")]
    out = _run("waterfall_email", first_name="Jane", last_name="Doe", domain="acme.com",
               providers=["pipecorn"])
    assert out["provider"] == "pipecorn"


_PROFIL = {"found": True, "full_name": "Jane Doe", "title": "VP Sales",
           "lk_headline": "VP Sales at Acme", "location": "Paris, France",
           "linkedin_profile_url": "https://www.linkedin.com/in/jane-doe",
           "positions": [{"title": "Head of Sales", "current": False, "company_name": "Old"},
                         {"title": "VP Sales", "current": True, "company_name": "Acme",
                          "company_domain": "acme.com"}]}
_SOCIETE = {"found": True, "company_name": "Acme", "domain": "acme.com",
            "industry": "Software", "employee_count": 248, "employee_range": "201-500",
            "headquarters": {"country": "France", "city": "Paris"},
            "linkedin_url": "https://www.linkedin.com/company/acme"}


@pytest.mark.parametrize("ns, kwargs", [
    ("waterfall_person", {"linkedin_url": "https://www.linkedin.com/in/jane-doe/"}),
    ("waterfall_company", {"domain": "acme.com"}),
    ("waterfall_tech", {"domain": "acme.com"}),
])
@pytest.mark.parametrize("mode", ["platform", "tenant"])
def test_pipecorn_synchrone_hors_de_la_cle_partagee(calls, ns, kwargs, mode):
    """Une recherche synchrone n'entre pas au budget d'unités : sautée AVANT l'appel sur
    toute clé qui n'est pas celle de l'appelant."""
    calls["modes"] = {"pipecorn": mode}
    out = _run(ns, providers=["pipecorn"], **kwargs)
    assert out["attempts"][0]["code"] == "own_key_required"
    assert "pipecorn" not in _made(calls)


def test_pipecorn_personne_lit_le_poste_en_cours(calls, horloge):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result=_PROFIL)
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               providers=["pipecorn"])
    assert out["provider"] == "pipecorn"
    assert out["person"]["title"] == "VP Sales" and out["person"]["company_name"] == "Acme"
    assert out["person"]["company_domain"] == "acme.com"
    assert calls["made"][0][1] == {"op": "person",
                                   "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
                                   "timeout_s": 40}


def test_pipecorn_personne_par_le_nom_a_la_societe(calls, horloge):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result=_PROFIL)
    _run("waterfall_person", first_name="Jane", last_name="Doe", company_name="Acme",
         providers=["pipecorn"])
    assert calls["made"][0][1] == {"op": "person", "firstname": "Jane", "lastname": "Doe",
                                   "company_name": "Acme", "timeout_s": 40}


def test_pipecorn_personne_introuvable_passe_la_main(calls):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result={"found": False})
    out = _run("waterfall_person", linkedin_url="https://www.linkedin.com/in/jane-doe/",
               providers=["pipecorn", "apollo"])
    assert out["attempts"][0]["status"] == "no_match"


def test_pipecorn_societe_par_domaine(calls, horloge):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result=_SOCIETE)
    out = _run("waterfall_company", domain="acme.com", providers=["pipecorn"])
    assert out["provider"] == "pipecorn"
    assert out["company"]["employees"] == 248 and out["company"]["city"] == "Paris"
    assert calls["made"][0][1] == {"op": "company", "domain": "acme.com", "timeout_s": 40}


def test_pipecorn_societe_par_nom_ecarte_l_homonyme(calls):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result={**_SOCIETE,
                                                                "company_name": "Zenith"})
    out = _run("waterfall_company", company_name="Acme", country="US",
               providers=["pipecorn"])
    assert out["found"] is False


def test_pipecorn_techno_noms_ou_confiance(calls, horloge):
    calls["modes"] = {"pipecorn": "org"}
    calls["answers"]["pipecorn"] = IssueCible(ok=True, result={
        "found": True, "technologies_with_confidence": [
            {"name": "HubSpot", "confidence": "high"}, {"name": "React", "confidence": 0.9}]})
    out = _run("waterfall_tech", domain="acme.com", providers=["pipecorn"])
    assert out["provider"] == "pipecorn"
    assert out["tech"]["technologies"] == ["HubSpot", "React"]
    assert calls["made"][0][1] == {"op": "stack", "domain": "acme.com", "timeout_s": 40}


# --- pipecorn synchrone : borné au temps qui reste dans la cascade ----------------------

@pytest.mark.parametrize("ns, kwargs", [
    ("waterfall_person", {"linkedin_url": "https://www.linkedin.com/in/jane-doe/"}),
    ("waterfall_company", {"domain": "acme.com"}),
    ("waterfall_tech", {"domain": "acme.com"}),
])
def test_pipecorn_synchrone_attend_ce_qui_reste_du_budget(calls, horloge, monkeypatch,
                                                          ns, kwargs):
    """Une étape payée avant lui a pris 28,6 s sur 40 : Pipecorn reçoit `timeout_s` 11,
    jamais les 60 s de son client."""
    from oto_mcp.waterfall import moteur
    avant, outil = {"waterfall_person": ("apollo", "apollo_match_person"),
                    "waterfall_company": ("apollo", "apollo_enrich_organization"),
                    "waterfall_tech": ("theirstack", "theirstack_companies_search")}[ns]
    calls["modes"] = {"pipecorn": "org", outil: "org"}
    vrai = moteur.appel._call

    async def lent(mcp, sub, tool, args, attempt):
        if tool == outil:
            horloge["now"] += 28.6
        return await vrai(mcp, sub, tool, args, attempt)
    monkeypatch.setattr(moteur.appel, "_call", lent)
    _run(ns, providers=[avant, "pipecorn"], **kwargs)
    envoye = [a for n, a in calls["made"] if n == "pipecorn"]
    assert envoye and envoye[0]["timeout_s"] == 11


@pytest.mark.parametrize("ns, kwargs", [
    ("waterfall_person", {"linkedin_url": "https://www.linkedin.com/in/jane-doe/"}),
    ("waterfall_company", {"domain": "acme.com"}),
    ("waterfall_tech", {"domain": "acme.com"}),
])
def test_pipecorn_synchrone_saute_sans_appel_quand_il_reste_trop_peu(calls, horloge,
                                                                     monkeypatch, ns, kwargs):
    """Moins de `BOUNDED_MIN_S` restantes : sautée `time_budget`, rien d'envoyé, rien de
    facturé — plutôt qu'une recherche coupée après dépense."""
    from oto_mcp.waterfall import moteur
    calls["modes"] = {"pipecorn": "org"}
    monkeypatch.setattr(moteur, "DEADLINE_S", moteur.BOUNDED_MIN_S - 0.5)
    out = _run(ns, providers=["pipecorn"], **kwargs)
    assert out["attempts"][0]["status"] == "skipped"
    assert out["attempts"][0]["code"] == "time_budget"
    assert "pipecorn" not in _made(calls)


def test_un_depassement_pipecorn_est_un_echec_nomme_de_l_etape(calls, horloge):
    """Le refus `pipecorn_timeout` de l'outil, tel que `executer_cible` le rend (un
    `INVALID_PARAMS` classé `invalid_input`, le nom dans le message) : l'étape ÉCHOUE,
    jamais un non-trouvé ni un trouvé ; la cascade passe à la suivante."""
    calls["modes"] = {"pipecorn": "org", "apollo_enrich_organization": "org"}
    calls["answers"]["pipecorn"] = IssueCible(
        ok=False, code="invalid_input",
        message="Refusal `pipecorn_timeout`: Pipecorn op='company': no answer within 40 s; "
                "Pipecorn may have billed the lookup. Do not retry it blindly.")
    out = _run("waterfall_company", domain="acme.com", providers=["pipecorn", "apollo"])
    by = {a["provider"]: a for a in out["attempts"]}
    assert by["pipecorn"]["status"] == "failed"
    assert "pipecorn_timeout" in by["pipecorn"]["message"]
    assert "may have billed" in by["pipecorn"]["message"]
    assert "apollo_enrich_organization" in _made(calls)


def test_le_plan_a_blanc_ne_pose_pas_le_delai(calls):
    """`timeout_s` se calcule à l'appel : le plan à blanc montre les arguments de la table."""
    calls["modes"] = {"pipecorn": "org"}
    out = _run("waterfall_company", domain="acme.com", providers=["pipecorn"], dry_run=True)
    assert out["attempts"][0]["arguments"] == {"op": "company", "domain": "acme.com"}
