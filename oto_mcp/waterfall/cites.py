"""Ce qu'une cascade appelle pour l'agent, et ce que l'agent doit encore appeler lui-même.

Chaque étape d'une cascade est un appel ordinaire de l'outil de son fournisseur, avec SES
gardes. Sous un travail hébergé, la liste d'outils du travail (`outils_du_travail`) dit ce
que le MODÈLE peut appeler ; une cascade accordée couvre les outils de ses propres étapes
(`outils_internes`, `outils_du_travail.ouvrir_composite`) : une table fixée dans le code, que
ni le modèle ni une procédure n'étendent — `providers=` ne fait que choisir parmi elles.
Activation du connecteur, niveau d'accès et clé restent ceux de l'étape.

Né du 26/10/2026, repris le 18/11/2026 : un agent dont la liste portait une cascade voyait
chaque étape refusée (`tool_not_in_job`) faute d'en lister l'outil ; la parade d'alors
proposait d'ajouter ces outils à la liste, ce qui les rendait appelables EN DIRECT, hors de
la cascade et de ses bornes (`max_paid_steps`, l'ordre, la qualification).

Ce qui reste à l'agent : une réponse `pending` (étape asynchrone pas finie à temps) se
COLLECTE plus tard par un appel que le modèle fait lui-même — l'outil de collecte de l'étape
(`collectes_de_l_etape`). Lui seul peut manquer à la liste d'un agent qui a la cascade. On
ne lit que les fournisseurs ÉCRITS à côté d'un appel d'un contact (`providers=[…]`) :
sans eux, l'ordre se résout au run, et l'inférer mettrait toute la table dans
`missing_tools`. Le mode tableau (`_act`) ne laisse jamais de réponse `pending` (ses étapes
asynchrones sont écartées) : il ne cite rien. Un plancher, jamais un plafond.
"""
from __future__ import annotations

import re

from .etape import Step
from .plan import STEPS_BY_NAMESPACE
from .qualification import _QUALIFIABLE

#: Un appel de cascade cité dans la prose : l'outil d'un contact ou son mode tableau (`_act`).
_CASCADE = re.compile(r"\b(" + "|".join(STEPS_BY_NAMESPACE) + r")(_act)?\b")
#: Les outils de cascade (un contact, ou le mode tableau `_act`) : un agent qui n'en a
#: aucun n'appellera aucune étape.
CASCADES = frozenset(STEPS_BY_NAMESPACE) | frozenset(f"{n}_act" for n in STEPS_BY_NAMESPACE)
#: Les fournisseurs écrits à côté : `providers=["apollo", "aiark"]` (ou `providers: [...]`).
_FOURNISSEURS = re.compile(r"""\bproviders\s*[=:]\s*\[([^\]]*)\]""")
_CLE = re.compile(r"[a-z][a-z0-9_]*")
#: Jusqu'où, après le nom de la cascade, `providers=` appartient encore à SON appel : un
#: appel écrit en entier tient en quelques centaines de caractères.
_PORTEE = 800
#: L'outil de la passe de qualification (`qualify=`), appelé par la cascade elle-même.
OUTIL_DE_QUALIFICATION = "jev_items"
#: Les outils de collecte qu'on PROPOSE à la personne qui pose un agent (`tools_to_add`) :
#: des lectures d'une soumission déjà faite, rien d'autre.
PROPOSABLES = frozenset({
    "apollo_reveal_phone_result", "dropcontact_result", "fullenrich_result",
    "lemlist_enrich_result",
})
#: Les outils de collecte jamais proposés : la collecte en est UNE opération, l'outil en
#: lance d'autres, facturées (`pipecorn` soumet aussi). Ils restent dans `missing_tools`,
#: au geste. `tests/test_outils_non_accordes.py` range chaque outil de collecte ici ou
#: dans `PROPOSABLES`.
NON_PROPOSES = frozenset({"pipecorn"})


def outils_de_l_etape(step: Step) -> list[str]:
    """Tout ce qu'une étape appelle : son outil, celui qui collecte une réponse tardive,
    celui qui révèle une trouvaille masquée, et ceux de ses variantes."""
    noms = [step.tool]
    if step.collect is not None:
        noms.append(step.collect.tool)
    if step.reveal:
        noms.append(step.reveal)
    for variante in step.variants:
        noms += outils_de_l_etape(variante)
    return noms


def outils_internes(ns: str) -> frozenset:
    """Tout ce que la cascade `ns` appelle elle-même : les outils de toutes ses étapes
    (collecte, révélation et variantes comprises), et la qualification là où elle se
    demande. Ce qu'une cascade accordée couvre sous un travail hébergé."""
    noms = {n for s in STEPS_BY_NAMESPACE.get(ns, ()) for n in outils_de_l_etape(s)}
    if ns in _QUALIFIABLE:
        noms.add(OUTIL_DE_QUALIFICATION)
    return frozenset(noms)


def collectes_de_l_etape(step: Step) -> list[str]:
    """L'outil qui collecte une réponse tardive de l'étape, et ceux de ses variantes."""
    noms = [step.collect.tool] if step.collect is not None else []
    for variante in step.variants:
        noms += collectes_de_l_etape(variante)
    return noms


def collectes_des_cascades(texte: str) -> list[tuple[str, str]]:
    """`[(cascade telle que citée, outil de collecte)]` : les outils qui collectent une
    réponse `pending` des étapes que la procédure nomme à côté d'un appel d'un contact
    (`waterfall_email(… providers=["fullenrich"])`), dans l'ordre écrit. Le mode tableau
    (`_act`) n'en laisse aucune. Une clé qu'aucune étape de cette cascade ne porte est
    ignorée : l'outil la refusera au run."""
    texte = texte or ""
    appels = list(_CASCADE.finditer(texte))
    cites: list[tuple[str, str]] = []
    for i, appel in enumerate(appels):
        if appel.group(2):
            continue
        fin = appels[i + 1].start() if i + 1 < len(appels) else len(texte)
        trouve = _FOURNISSEURS.search(texte, appel.end(), min(fin, appel.end() + _PORTEE))
        if trouve is None:
            continue
        etapes = {s.key: s for s in STEPS_BY_NAMESPACE[appel.group(1)]}
        for cle in _CLE.findall(trouve.group(1).lower()):
            if cle in etapes:
                cites += [(appel.group(0), nom) for nom in collectes_de_l_etape(etapes[cle])]
    return cites


def outils_des_cascades(texte: str) -> list[tuple[str, str]]:
    """`[(outil, qui l'appelle)]`, la forme de `missing_tools` (`needed_by`)."""
    return [(nom, f"`{cascade}` (pending answers)")
            for cascade, nom in collectes_des_cascades(texte)]
