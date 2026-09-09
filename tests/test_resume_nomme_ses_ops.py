"""Le résumé SERVI d'un outil multiplexé nomme ses ops — sinon il cache ce qu'il fait.

Constaté en production le 09/09/2026 : une utilisatrice se voyait proposer, tâche après
tâche, de créer un TABLEAU et jamais un PROJET — alors que la doctrine injectée dit
l'inverse (« tâche ad-hoc = crée un projet, jamais hors-sol », 10 mentions du projet
contre 3 du tableau). Le texte poussait le bon geste ; c'est le CATALOGUE qui ne le
rendait pas.

Le catalogue ne sert pas la description entière : `tool_registry.blurb()` en rend le
1er PARAGRAPHE borné — 100 caractères pour `oto_list_my_tools`, 140 pour le registre
boot — coupé à la dernière phrase complète. La description d'`oto_project` s'ouvrait
sur ~275 caractères à propos du champ `url` AVANT d'arriver à `op=create` : la coupure
tombait avant, et le résumé servi était

    « Projects (organization layer, ADR 0030 owned resource). »

`oto_project` était le SEUL conteneur de la plateforme dont le résumé servi cachait sa
création (`oto_org` et `oto_group` nomment `op=create` dès leur 2ᵉ phrase). Conséquence
mécanique, la recherche du catalogue étant LEXICALE sur ce résumé : une requête `create`
rendait `data_create_datastore` en tête et ne rendait pas `oto_project` du tout. L'agent
choisissait ce que le catalogue lui montrait.

⚠️ Le contrôle porte sur le résumé RÉELLEMENT PRODUIT à partir du MONTAGE (register_all
+ capacités), jamais sur le fichier : ce qui compte est la chaîne qui part sur le fil,
pas l'ordre des phrases dans le source (même exigence que
`test_description_dit_le_proprietaire.py`).

── Le balayage des AUTRES outils multiplexés (fait le 09/09/2026) ────────────────────
L'ensemble n'est PAS vide, et il est trop gros pour être un banc : sur 679 outils
montés, 285 portent un paramètre `op` ; 152 d'entre eux ne citent aucune de leurs ops
dans leur résumé à 140 caractères (168 à 100). La raison n'est pas un défaut : la
convention de maison des CONNECTEURS est de nommer les verbes en PROSE (« list, read,
create, delete », « lister, lire, créer ») plutôt qu'en `op=`, et beaucoup de ces
résumés sont en français quand les ops sont en anglais. Élargir l'invariant à ce
peuplement reviendrait à demander la réécriture de 150 connecteurs sous couvert de test.
Ce banc tient donc l'invariant là où il ENGAGE : les conteneurs de la plateforme — org,
équipe, projet — ceux dont on choisit UN pour y poser une tâche, et qui se comparent
entre eux dans le catalogue au moment du choix.
"""
from __future__ import annotations

import asyncio
import re

import pytest
from fastmcp import FastMCP

from oto_mcp import tool_registry
from oto_mcp.tools.meta import _CATALOG_BLURB


# Les deux budgets RÉELLEMENT servis, pas un budget de test : 100 = la ligne de
# `oto_list_my_tools` (celle que la recherche fouille), 140 = l'entrée du registre boot.
_BUDGETS = (_CATALOG_BLURB, 140)

# Les conteneurs de la plateforme : les objets dans lesquels une tâche peut vivre.
# On les tient ENSEMBLE parce que c'est ensemble qu'ils sont lus, au moment où l'agent
# choisit où poser ce qu'on lui demande.
_CONTENEURS = ("oto_org", "oto_group", "oto_project")


def _descriptions_montees() -> dict[str, str]:
    """Les descriptions telles que le MONTAGE les sert — espaces normalisés."""
    from oto_mcp.capabilities import _mcp_adapter, registry
    from oto_mcp.tools import register_all

    mcp = FastMCP("sonde-resume")
    register_all(mcp)
    _mcp_adapter.register(mcp, registry.CAPABILITIES)

    async def _lire():
        return {t.name: " ".join((t.description or "").split())
                for t in await mcp.list_tools(run_middleware=False)}

    return asyncio.run(_lire())


@pytest.fixture(scope="module")
def montees() -> dict[str, str]:
    return _descriptions_montees()


@pytest.mark.parametrize("nom", _CONTENEURS)
@pytest.mark.parametrize("budget", _BUDGETS)
def test_un_conteneur_nomme_ses_ops_dans_le_resume_servi(montees, nom, budget):
    """`op=` doit tenir DANS le résumé, à tous les budgets servis. Un conteneur dont le
    résumé n'annonce que son identité laisse l'agent conclure qu'il ne fait rien."""
    assert nom in montees, f"{nom} n'est plus monté — ce banc n'a plus d'objet"
    resume = tool_registry.blurb(montees[nom], budget)
    assert re.search(r"\bop=", resume), (
        f"le résumé servi de {nom} à {budget} c. ne nomme aucune op : {resume!r}. "
        f"Remonte l'énumération des ops dans le 1er paragraphe — ce qui suit la "
        f"coupure n'atteint jamais le catalogue.")


@pytest.mark.parametrize("budget", _BUDGETS)
def test_oto_project_annonce_sa_CREATION(montees, budget):
    """Le défaut précis du 09/09/2026 : la création cachée derrière la phrase `url`."""
    resume = tool_registry.blurb(montees["oto_project"], budget)
    assert "op=create" in resume, (
        f"le résumé servi d'oto_project ne dit plus qu'on peut créer un projet : "
        f"{resume!r}")


def test_le_champ_url_est_toujours_dit(montees):
    """Réordonner n'est pas supprimer : la phrase descendue reste servie, entière."""
    d = montees["oto_project"]
    assert "EVERY project carries `url`" in d
    assert "never rebuild one from a pattern" in d
    assert "ADR 0030 owned resource" in d


def test_le_catalogue_RETROUVE_oto_project_sur_create(montees):
    """Le mécanisme réel du défaut, pas sa cause : la recherche du catalogue est
    LEXICALE sur le résumé de 100 caractères. Un résumé qui tait la création rend
    l'outil introuvable par le mot qui la nomme — et l'agent prend celui qu'il voit."""
    entries = [{"name": n, "description": tool_registry.blurb(d, _CATALOG_BLURB),
                "namespace_help": ""}
               for n, d in montees.items()]
    noms = [e["name"] for e in tool_registry.match("create", entries)]
    assert "oto_project" in noms, (
        "une recherche `create` ne rend pas oto_project : le catalogue propose un "
        "tableau et jamais un projet, quoi que dise la doctrine injectée")


def test_le_detecteur_MORD():
    """Preuve de morsure : le texte D'AVANT, mot pour mot, doit échouer aux deux
    budgets. Sans ça, le banc dirait vert d'un ordre quelconque."""
    avant = (
        "Projects (organization layer, ADR 0030 owned resource). EVERY project carries "
        "`url` — the web address to OPEN it, in the reader's own product; hand it over "
        "as-is when asked \"where is it?\", never rebuild one from a pattern (`null` = "
        "that reader's product has no such view). op=create (name, optional brief_md; "
        "owner_type user|org + owner_id for a team project) / list (ORG-SCOPED: …)")
    for budget in _BUDGETS:
        assert "op=" not in tool_registry.blurb(avant, budget), (
            f"à {budget} c., l'ancien ordre passerait le banc — il ne mesure rien")
