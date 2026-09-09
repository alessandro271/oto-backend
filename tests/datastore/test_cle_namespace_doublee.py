"""La clé `namespace` survit au renommage, le temps du préavis.

⚠️ **La seule panne MUETTE de toute la bascule `namespace` → `datastore`.** Un chemin
qui change rend un 404 ou un 308 : on le voit, on le corrige le jour même. Une clé de
réponse qui disparaît ne rend rien — `r.namespace` vaut `undefined`, sans erreur et sans
journal, chez un consommateur qu'on ne connaît peut-être même pas.

Repérée par le contrôle de contrat des fronts sur `GET …/schema` : épinglé
`{enforced, namespace, schema, warning}`, servi `{datastore, enforced, ns_id, schema,
warning}`. Les trois autres réponses touchées n'AJOUTENT que des clés — celle-ci est la
seule à en retirer une.
"""
from __future__ import annotations

from oto_mcp import deprecations
from oto_mcp.datastore.identite import de_releve, identite


def test_les_deux_noms_sont_servis_et_portent_la_meme_valeur():
    out = identite(42, "vivier")
    assert out["datastore"] == "vivier"
    assert out["namespace"] == "vivier", (
        "un consommateur qui lit l'ancien nom reçoit `undefined` sans erreur — "
        "c'est la seule panne silencieuse de la bascule")
    assert out["ns_id"] == 42


def test_le_doublage_vaut_aussi_pour_les_reponses_bati_sur_un_releve():
    out = de_releve({"ns_id": 7, "datastore": "leads"})
    assert out["namespace"] == out["datastore"] == "leads"


def test_le_doublage_porte_une_DATE_et_c_est_celle_des_alias():
    """⚠️ Un doublage sans date est un second nom permanent : ça se décide, ça ne
    s'ajoute pas. Celui-ci s'en va avec les alias de chemin, à la même date."""
    assert deprecations.RETRAIT_DATASTORE.strftime("%d/%m/%Y") == "08/11/2026"


def test_une_adresse_non_resolue_double_aussi():
    """Le repli `adresse` sert quand rien n'a été relevé — il doit répondre aux deux
    noms comme le reste, sinon le consommateur qui lit l'ancien voit `undefined` dans
    le cas précisément où il cherche à comprendre ce qui a échoué."""
    out = identite(None, None, adresse="tableau-inconnu")
    assert out["namespace"] == out["datastore"] == "tableau-inconnu"
    assert out["ns_id"] is None
