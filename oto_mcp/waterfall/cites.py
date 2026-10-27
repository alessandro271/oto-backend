"""Les outils qu'une cascade appellera pour une procédure qui la lance — lus dans sa prose,
jamais devinés.

Chaque étape d'une cascade est un appel ordinaire de l'outil de son fournisseur, avec SES
gardes, dont la liste d'outils d'un travail hébergé (`outils_du_travail`, voir le module du
paquet). Un agent qui a `waterfall_email_act` sans `apollo_match_person` voit donc chaque
étape Apollo refusée, et la ligne revient sans réponse (`no_step_answered`) : rien ne disait,
sur l'agent, qu'il lui manquait l'outil.

Né du 26/10/2026 : un agent hébergé dont la procédure lançait `waterfall_email_act` avec
`providers=["apollo", "aiark"]` a passé deux runs sans trouver une adresse, chaque étape
refusée au travail, et a conclu à un quota épuisé.

On ne lit que les fournisseurs ÉCRITS à côté de l'appel (`providers=[…]`, comme le prend
l'outil) : sans eux, l'ordre se résout au run (le réglage de la carte, sinon la table par
défaut, et seulement les fournisseurs dont une clé existe) — l'inférer ici mettrait toute
la table dans `missing_tools`, et une liste trop large est exactement ce que la liste d'un
agent existe pour empêcher. Comme pour les recettes : un plancher, jamais un plafond.
"""
from __future__ import annotations

import re

from .etape import Step
from .plan import STEPS_BY_NAMESPACE

#: Un appel de cascade cité dans la prose : l'outil d'un contact ou son mode tableau (`_act`).
_CASCADE = re.compile(r"\b(" + "|".join(STEPS_BY_NAMESPACE) + r")(?:_act)?\b")
#: Les fournisseurs écrits à côté : `providers=["apollo", "aiark"]` (ou `providers: [...]`).
_FOURNISSEURS = re.compile(r"""\bproviders\s*[=:]\s*\[([^\]]*)\]""")
_CLE = re.compile(r"[a-z][a-z0-9_]*")
#: Jusqu'où, après le nom de la cascade, `providers=` appartient encore à SON appel : un
#: appel écrit en entier tient en quelques centaines de caractères.
_PORTEE = 800


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


def outils_des_cascades(texte: str) -> list[tuple[str, str]]:
    """`[(outil, qui l'appelle)]` : les outils des étapes que la procédure nomme à côté
    d'une cascade (`waterfall_email_act(… providers=["apollo"])`), dans l'ordre écrit. Une
    clé qu'aucune étape de cette cascade ne porte est ignorée : l'outil la refusera au run,
    ce n'est pas un outil qui manque."""
    texte = texte or ""
    appels = list(_CASCADE.finditer(texte))
    cites: list[tuple[str, str]] = []
    for i, appel in enumerate(appels):
        fin = appels[i + 1].start() if i + 1 < len(appels) else len(texte)
        trouve = _FOURNISSEURS.search(texte, appel.end(), min(fin, appel.end() + _PORTEE))
        if trouve is None:
            continue
        etapes = {s.key: s for s in STEPS_BY_NAMESPACE[appel.group(1)]}
        par = f"`{appel.group(0)}` (providers)"
        for cle in _CLE.findall(trouve.group(1).lower()):
            if cle in etapes:
                cites += [(nom, par) for nom in outils_de_l_etape(etapes[cle])]
    return cites
