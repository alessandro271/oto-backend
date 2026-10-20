"""Les gens d'une société (`waterfall/gens.py`, `vivier.py`, `qualification.py`) — des
noms masqués ne stoppent pas la cascade ; une personne n'est gardée que si sa société
ACTUELLE est celle demandée ; Jev juge ce qui a été trouvé (jamais un nom ni une URL), et
un `qualify` que Jev ne peut pas servir est refusé AVANT toute dépense ; la recherche
gratuite est lue page après page, une liste payée achète ses pages suivantes jusqu'à
`limit` retenus."""
import pytest
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.meta import IssueCible
from oto_mcp.waterfall import vivier

from _waterfall_faux import (_FIT, _gens, _made, _run, _verdicts,  # noqa: F401
                             calls)


# --- les gens d'une société (`waterfall_people`) ----------------------------------

def test_people_apollo_garde_les_roles_et_l_id_de_revelation(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a1", "first_name": "Jane", "last_name_obfuscated": "Do***e", "title": "VP Sales"},
        {"id": "a2", "first_name": "Bob", "last_name_obfuscated": "Sm***h", "title": "Engineer"}]})
    out = _run("waterfall_people", domain="acme.com", roles="sales, CEO", country="France")
    assert out["provider"] == "apollo" and out["people"]["count"] == 1
    assert out["people"]["people"][0] == {"full_name": "Jane Do***e", "title": "VP Sales",
                                          "apollo_id": "a1", "name_obfuscated": True}
    assert calls["made"][0][1] == {"domains": ["acme.com"], "titles": ["sales", "CEO"],
                                   "person_locations": ["France"], "per_page": 20}


def test_people_employes_linkedin_ecarte_ceux_d_ailleurs(calls):
    calls["answers"]["apify_run_sync"] = IssueCible(ok=True, result=[
        {"firstName": "Li", "lastName": "Wei", "linkedinUrl": "https://linkedin.com/in/liwei",
         "currentPosition": [{"companyUniversalName": "acme-printers", "position": "VP of B2B BU"}]},
        {"firstName": "Fake", "lastName": "Profile", "linkedinUrl": "https://linkedin.com/in/fake",
         "currentPosition": [{"companyUniversalName": "someone-else", "position": "CEO"}]}])
    out = _run("waterfall_people", company_linkedin_url="https://www.linkedin.com/company/acme-printers/",
               providers=["linkedin_employees"])
    assert [p["full_name"] for p in out["people"]["people"]] == ["Li Wei"]
    assert out["people"]["people"][0]["title"] == "VP of B2B BU"
    assert calls["made"][0][1]["run_input"]["companies"] == ["https://www.linkedin.com/company/acme-printers/"]


def test_people_google_lit_le_titre_et_exige_la_societe(calls):
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Zhang San - VP of Overseas Business - Acme Additive | LinkedIn",
         "link": "https://cn.linkedin.com/in/zhangsan?trk=x", "snippet": ""},
        {"title": "John Roe - Director - Other Corp | LinkedIn",
         "link": "https://linkedin.com/in/johnroe", "snippet": ""}]})
    out = _run("waterfall_people", company_name="Acme Additive", providers=["google"])
    assert out["people"]["people"] == [{"full_name": "Zhang San", "title": "VP of Overseas Business",
                                        "linkedin_url": "https://cn.linkedin.com/in/zhangsan"}]
    assert calls["made"][0][1]["site_filter"] == "linkedin.com/in"


def test_people_page_linkedin_malformee_refusee(calls):
    with pytest.raises(McpError, match="LinkedIn company page"):
        _run("waterfall_people", company_linkedin_url="https://example.com/acme")


def test_des_noms_masques_ne_stoppent_pas_la_cascade_mais_restent_le_repli(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a1", "first_name": "Jane", "last_name_obfuscated": "Do***e", "title": "VP Sales"}]})
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Jane Doe - VP Sales - Acme | LinkedIn",
         "link": "https://linkedin.com/in/janedoe", "snippet": ""}]})
    out = _run("waterfall_people", domain="acme.com", company_name="Acme",
               providers=["apollo", "google"])
    assert out["provider"] == "google"
    assert out["people"]["people"][0]["full_name"] == "Jane Doe"
    assert [a["status"] for a in out["attempts"]] == ["weak", "found"]

    calls["made"].clear()
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": []})
    out = _run("waterfall_people", domain="acme.com", company_name="Acme",
               providers=["apollo", "google"])
    # Rien de mieux : les noms masqués d'Apollo sont la réponse.
    assert out["found"] is True and out["provider"] == "apollo"
    assert out["people"]["people"][0]["apollo_id"] == "a1"


def test_on_ne_demande_pas_plus_que_ce_que_l_etape_garde(calls):
    _run("waterfall_people", domain="acme.com", providers=["aiark"], limit=7)
    assert calls["made"][0][1]["size"] == 7
    calls["made"].clear()
    _run("waterfall_company", domain="acme.com", providers=["theirstack", "aiark"])
    args = dict(calls["made"])
    assert args["theirstack_companies_search"]["limit"] == 1
    assert args["linkedin_aiark_search"]["size"] == 1


def test_reel_google_lit_le_sous_titre_et_ecarte_la_simple_mention(calls):
    calls["answers"]["serper_search"] = IssueCible(ok=True, result={"organic": [
        {"title": "Dr. Ulla Jay – Acme Marking GmbH",
         "subtitle": "Paderborn, Deutschland · Acme Marking GmbH",
         "link": "https://de.linkedin.com/in/ulla-jay-1"},
        {"title": "Phil Bee – TRACEABLE AND ...",
         "subtitle": "Paderborn, Deutschland · CTO - Acme Marking · Acme Marking GmbH",
         "link": "https://de.linkedin.com/in/phil-bee-2"},
        {"title": "Dr Paul Roe - Senior Project Manager", "link": "https://uk.linkedin.com/in/paul-roe",
         "snippet": "... Acme Marking Techniques Deposition PECVD ..."}]})
    out = _run("waterfall_people", company_name="Acme Marking", providers=["google"])
    people = out["people"]["people"]
    # Le CTO d'abord (séniorité), la simple mention dans un extrait écartée.
    assert [p["full_name"] for p in people] == ["Phil Bee", "Dr. Ulla Jay"]
    assert people[0]["title"] == "CTO - Acme Marking" and people[1].get("title") is None


def test_reel_apollo_people_dit_qui_a_un_email(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "a2", "first_name": "Dev", "last_name_obfuscated": "Pi***k", "title": "Software Developer",
         "has_email": True, "has_direct_phone": "Maybe: please request direct dial via people/bulk_match"},
        {"id": "a1", "first_name": "Ulla", "last_name_obfuscated": "Ja***y", "title": "CEO, Co-Founder",
         "has_email": True, "has_direct_phone": "Yes"}]})
    out = _run("waterfall_people", domain="acme.de", providers=["apollo"])
    first = out["people"]["people"][0]
    assert first["apollo_id"] == "a1" and first["has_email"] is True and first["has_direct_phone"] is True


# --- qualification par Jev (optionnelle) ----------------------------------------------

def test_qualifier_annote_chaque_personne_sans_rien_ecarter_par_defaut(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT)
    people = out["people"]["people"]
    assert [p["fit"]["fit"] for p in people] == [0.95, 0.10]   # vue resserrée par défaut
    assert out["qualification"]["status"] == "done" and out["qualification"]["model"] == "jev-2026-09"
    # Seuls les champs d'un jugement partent chez Jev : jamais un nom ni une URL.
    sent = dict(calls["made"])["jev_items"]["items"]
    assert sent[0]["state"] == {"title": "CEO", "location": "Lyon", "company": "Acme"}


def test_qualify_min_ecarte_les_personnes_sous_le_seuil(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7)
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann Boss"]
    assert out["people"]["count"] == 1 and out["qualification"]["not_qualified"] == 1
    assert out["attempts"][0]["qualified"] == 1 and out["attempts"][0]["not_qualified"] == 1
    # Les écartés reviennent toujours, en compact : vérifier la rubrique ne repaie rien.
    assert out["unqualified"] == [{"full_name": "Bob Dev", "title": "Developer",
                                   "linkedin_url": "https://fr.linkedin.com/in/bob-dev",
                                   "answers": {"fit": 0.1}}]


@pytest.mark.parametrize("mode", ["forbidden", "over_quota", "org", "user"])
@pytest.mark.parametrize("ns,kw", [("waterfall_people", {"company_name": "Acme"}),
                                   ("waterfall_company", {"domain": "acme.com"})])
def test_sans_jev_utilisable_qualify_est_refuse_avant_toute_depense(calls, mode, ns, kw):
    """Des résultats non jugés se liraient comme jugés (et le mode tableau les écrirait
    `found`) : `qualify` demandé alors que Jev ne peut pas tourner — pas de clé partagée,
    ou une clé plus proche qui la masque — est refusé nommément, rien n'est appelé."""
    _gens(calls)
    calls["modes"] = {"jev_items": mode}
    with pytest.raises(McpError) as e:
        _run(ns, qualify=_FIT, **kw)
    assert e.value.error.data["code"] == "qualification_unavailable"
    assert calls["made"] == []


def test_jev_qui_refuse_a_l_appel_rend_les_resultats_non_qualifies(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = McpError(ErrorData(code=INVALID_PARAMS,
                                                       message="No `jev` key for your org"))
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7)
    assert out["found"] is True and out["people"]["count"] == 2
    assert out["qualification"]["status"] == "skipped"


def test_une_societe_qualifiee_reste_trouvee_et_dit_si_elle_passe(calls):
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [{
        "summary": {"name": "Acme", "industry": "machinery"}, "link": {"domain": "acme.com"}}]})
    calls["answers"]["jev_items"] = _verdicts(0.2)
    out = _run("waterfall_company", domain="acme.com", providers=["aiark"],
               qualify=_FIT, qualify_min=0.7)
    assert out["found"] is True and out["company"]["qualification"]["passed"] is False
    assert dict(calls["made"])["jev_items"]["items"][0]["state"]["industry"] == "machinery"


def test_pas_de_qualification_sans_resultat_ni_en_dry_run(calls):
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT)
    assert out["found"] is False and "jev_items" not in _made(calls)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT,
               dry_run=True)
    assert out["qualification"]["status"] == "planned"


@pytest.mark.parametrize("kwargs", [{"qualify_min": 0.7}, {"qualify": {}},
                                    {"qualify": _FIT, "qualify_min": 1.5},
                                    {"qualify": {"x": {"type": "score"}}, "qualify_min": 0.5}])
def test_une_rubrique_mal_formee_est_refusee(calls, kwargs):
    with pytest.raises(McpError):
        _run("waterfall_people", company_name="Acme", **kwargs)


def test_si_personne_ne_passe_le_fournisseur_suivant_est_essaye(calls):
    _gens(calls)
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": "Cleo Head", "title": "Head of Marketing"},
         "link": {"linkedin": "https://www.linkedin.com/in/cleo-head"}}]})
    calls["answers"]["jev_items"] = [_verdicts(0.2, 0.1), _verdicts(0.9)]
    out = _run("waterfall_people", company_name="Acme", domain="acme.com",
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert out["provider"] == "aiark"
    assert [p["full_name"] for p in out["people"]["people"]] == ["Cleo Head"]
    assert out["attempts"][0]["status"] == "unqualified"
    assert out["attempts"][0]["not_qualified"] == 2
    q = out["qualification"]
    assert q["calls"] == 2 and q["not_qualified"] == 2


def test_show_unqualified_rend_les_ecartes_avec_leurs_reponses(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.2, 0.1)
    out = _run("waterfall_people", company_name="Acme", providers=["google"],
               qualify=_FIT, qualify_min=0.7, show_unqualified=True)
    assert out["found"] is False
    assert [p["full_name"] for p in out["unqualified"]] == ["Ann Boss", "Bob Dev"]
    assert out["unqualified"][0]["qualification"]["answers"]["fit"]["noul"] == 0.2
    assert out["unqualified"][0]["location"] == "Lyon"     # the full record


def test_un_ecarte_retrouve_par_le_fournisseur_suivant_n_est_pas_rejuge(calls):
    _gens(calls)
    calls["answers"]["linkedin_aiark_search"] = IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": "Bob Dev", "title": "Developer"},
         "link": {"linkedin": "https://fr.linkedin.com/in/bob-dev/"}}]})
    calls["answers"]["jev_items"] = _verdicts(0.2, 0.1)
    out = _run("waterfall_people", company_name="Acme", domain="acme.com",
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert _made(calls).count("jev_items") == 1
    assert out["found"] is False and out["qualification"]["not_qualified"] == 2
    assert out["attempts"][1]["status"] == "unqualified"


def test_moins_de_qualifies_que_demande_s_arrete_et_propose_la_suite(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", domain="acme.com", limit=5,
               providers=["google", "aiark"], qualify=_FIT, qualify_min=0.7)
    assert out["provider"] == "google" and "linkedin_aiark_search" not in _made(calls)
    assert out["continue_with"]["providers"] == ["aiark"]
    assert out["continue_with"]["short_of_limit"] == 4


def test_reel_recherche_apollo_gratuite_jugee_puis_seuls_les_retenus_reveles(calls):
    """Le cas d'une grande enseigne (19/10/2026) : 239 personnes à la recherche gratuite,
    titres visibles, noms masqués — Jev juge les titres, on ne révèle que les meilleurs
    qui passent ; AI Ark (payé au profil) n'est jamais appelé."""
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "p1", "first_name": "Bo", "last_name_obfuscated": "D***c",
         "title": "Directeur Marketing, Communication, Omnicommerce"},
        {"id": "p2", "first_name": "Ba", "last_name_obfuscated": "V***n",
         "title": "Customer Experience Leader"},
        {"id": "p3", "first_name": "Ev", "last_name_obfuscated": "E***t",
         "title": "Responsable des Événements de Communication"}]})
    calls["answers"]["jev_items"] = _verdicts(0.82, 0.78, 0.22)
    calls["answers"]["apollo_match_person"] = [
        IssueCible(ok=True, result={"person": {"name": "Bo Dee", "title": "Directeur Marketing",
                                               "linkedin_url": "http://www.linkedin.com/in/bo-dee",
                                               "email": "bo@acme.fr", "email_status": "verified"}}),
        IssueCible(ok=True, result={"person": {"name": "Ba Vee", "title": "Customer Experience Leader",
                                               "linkedin_url": "http://www.linkedin.com/in/ba-vee",
                                               "email": "ba@acme.fr", "email_status": "verified"}})]
    out = _run("waterfall_people", domain="acme.fr", limit=2, qualify=_FIT, qualify_min=0.7,
               providers=["apollo", "aiark"])
    made = _made(calls)
    assert made.count("apollo_match_person") == 2 and "linkedin_aiark_search" not in made
    assert made.count("jev_items") == 1
    people = out["people"]["people"]
    assert [p["full_name"] for p in people] == ["Bo Dee", "Ba Vee"]
    assert people[0]["email"] == "bo@acme.fr" and people[0]["fit"] == {"fit": 0.82}
    a = out["attempts"][0]
    assert a["status"] == "found" and a["revealed"] == 2 and a["reveals_paid"] == 2
    assert out["qualification"]["not_qualified"] == 1
    # Les révélations sont payées en série, par id — jamais par nom.
    assert [args for n, args in calls["made"] if n == "apollo_match_person"] == [
        {"person_id": "p1"}, {"person_id": "p2"}]


def test_sans_rubrique_les_noms_masques_restent_un_repli(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": "p1", "first_name": "Bo", "last_name_obfuscated": "D***c", "title": "CMO"}]})
    out = _run("waterfall_people", domain="acme.fr", providers=["apollo"])
    assert "apollo_match_person" not in _made(calls)
    assert out["attempts"][0]["status"] == "weak" and out["found"] is True


def test_max_paid_steps_borne_aussi_les_revelations(calls):
    calls["answers"]["apollo_search_people"] = IssueCible(ok=True, result={"people": [
        {"id": f"p{i}", "first_name": f"X{i}", "last_name_obfuscated": "Y***", "title": "CMO"}
        for i in range(3)]})
    calls["answers"]["jev_items"] = _verdicts(0.9, 0.9, 0.9)
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {
        "name": "X Y", "linkedin_url": "http://www.linkedin.com/in/x"}})
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT,
               providers=["apollo"], max_paid_steps=2)
    assert _made(calls).count("apollo_match_person") == 2
    assert out["attempts"][0]["stopped"] == "max_paid_steps"


def _page(start, n, total):
    return IssueCible(ok=True, result={"total_entries": total, "people": [
        {"id": f"p{i}", "first_name": f"P{i}", "last_name_obfuscated": "X***",
         "title": "CMO" if i == start else "Developer"} for i in range(start, start + n)]})


def test_la_recherche_gratuite_est_lue_page_apres_page_jusqu_au_bout(calls):
    calls["answers"]["apollo_search_people"] = [_page(0, 100, 230), _page(100, 100, 230),
                                                _page(200, 30, 230)]
    calls["answers"]["jev_items"] = _verdicts(*([0.9] + [0.1] * 199))
    calls["answers"]["apollo_match_person"] = IssueCible(ok=True, result={"person": {
        "name": "P0 Y", "linkedin_url": "http://www.linkedin.com/in/p0"}})
    out = _run("waterfall_people", domain="acme.fr", limit=1, qualify=_FIT, qualify_min=0.7,
               providers=["apollo"])
    pages = [a.get("page", 1) for n, a in calls["made"] if n == "apollo_search_people"]
    assert pages == [1, 2, 3]
    a = out["attempts"][0]
    assert a["pages"] == 3 and a["total"] == 230 and "pool_truncated" not in a
    # 230 titres = deux appels Jev (200 + 30), jugés ensemble.
    jev = [args for n, args in calls["made"] if n == "jev_items"]
    assert [len(j["items"]) for j in jev] == [200, 30]
    assert out["qualification"]["not_qualified"] >= 199


def test_sans_temps_pour_juger_les_pages_s_arretent_et_le_recu_le_dit(calls, monkeypatch):
    monkeypatch.setattr(vivier, "_PAGES_RESERVE_S", 10_000)
    calls["answers"]["apollo_search_people"] = [_page(0, 100, 500)]
    out = _run("waterfall_people", domain="acme.fr", qualify=_FIT, providers=["apollo"])
    a = out["attempts"][0]
    assert a["pages"] == 1 and a["pool_truncated"] == "time_budget"


def test_sans_rubrique_une_seule_page(calls):
    calls["answers"]["apollo_search_people"] = [_page(0, 20, 500)]
    _run("waterfall_people", domain="acme.fr", providers=["apollo"])
    assert _made(calls).count("apollo_search_people") == 1


def _aiark_page(names_titles):
    return IssueCible(ok=True, result={"content": [
        {"profile": {"full_name": n, "title": t},
         "link": {"linkedin": f"https://www.linkedin.com/in/{n.lower().replace(' ', '-')}"}}
        for n, t in names_titles]})


def test_une_liste_payee_achete_ses_pages_suivantes_jusqu_a_limit_qualifies(calls):
    calls["answers"]["linkedin_aiark_search"] = [
        _aiark_page([("Ann A", "CMO"), ("Bob B", "Developer")]),
        _aiark_page([("Cy C", "Head of Marketing"), ("Di D", "Intern")]),
        _aiark_page([("Ed E", "Never reached")])]
    calls["answers"]["jev_items"] = [_verdicts(0.9, 0.1), _verdicts(0.8, 0.1)]
    out = _run("waterfall_people", domain="acme.fr", limit=2, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"])
    pages = [a.get("page", 0) for n, a in calls["made"] if n == "linkedin_aiark_search"]
    assert pages == [0, 1]                       # page 2 never bought: limit reached
    assert [p["full_name"] for p in out["people"]["people"]] == ["Ann A", "Cy C"]
    a = out["attempts"][0]
    assert a["pages"] == 2 and a["qualified"] == 2 and a["not_qualified"] == 2
    assert "continue_with" not in out


def test_les_pages_payees_comptent_dans_max_paid_steps(calls):
    calls["answers"]["linkedin_aiark_search"] = [
        _aiark_page([("Ann A", "Intern")]), _aiark_page([("Bob B", "Intern")]),
        _aiark_page([("Cy C", "Intern")])]
    calls["answers"]["jev_items"] = _verdicts(0.1)
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"], max_paid_steps=2)
    assert _made(calls).count("linkedin_aiark_search") == 2
    assert out["attempts"][0]["stopped"] == "max_paid_steps"


def test_la_fin_des_resultats_arrete_les_pages(calls):
    calls["answers"]["linkedin_aiark_search"] = [_aiark_page([("Ann A", "Intern")]),
                                                 IssueCible(ok=True, result={"content": []})]
    calls["answers"]["jev_items"] = _verdicts(0.1)
    out = _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, qualify_min=0.7,
               providers=["aiark"])
    assert _made(calls).count("linkedin_aiark_search") == 2
    assert out["attempts"][0]["pages"] == 2 and "stopped" not in out["attempts"][0]


def test_sans_seuil_une_liste_payee_ne_lit_qu_une_page(calls):
    calls["answers"]["linkedin_aiark_search"] = _aiark_page([("Ann A", "CMO")])
    calls["answers"]["jev_items"] = _verdicts(0.1)
    _run("waterfall_people", domain="acme.fr", limit=3, qualify=_FIT, providers=["aiark"])
    assert _made(calls).count("linkedin_aiark_search") == 1



def test_full_rend_chaque_personne_entiere(calls):
    _gens(calls)
    calls["answers"]["jev_items"] = _verdicts(0.95, 0.10)
    out = _run("waterfall_people", company_name="Acme", providers=["google"], qualify=_FIT,
               full=True)
    p = out["people"]["people"][0]
    assert p["qualification"]["answers"]["fit"]["noul"] == 0.95 and "fit" not in p
