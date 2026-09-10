"""Le DESSIN d'une procédure — la garde d'écriture (pendant de `slots.slots_check`).

Depuis l'issue #108 d'un front tiers, le schéma d'une procédure n'est plus une illustration :
c'est la **vue par défaut** de la page du process. Le front cherche UN bloc de code
non tagué (``` sans langage) tracé en caractères semi-graphiques, le reparse en graphe
et le redessine en cartes. Une procédure sans dessin s'affiche donc en état vide —
et un dessin hors grammaire est *refusé* par le parseur, qui retombe sur les caractères
bruts (le parseur préfère refuser plutôt que dessiner faux). Le dessin est une SECTION
REQUISE d'une procédure, pas une décoration.

Ce module ne reparse rien : la grammaire complète vit dans `src/lib/ascii-diagram.ts`
côté front, et la redoubler ici fabriquerait deux vérités qui divergeraient au premier
changement de rendu. On garde le seul test qui ne peut pas mentir dans les deux sens :
**« l'auteur a-t-il dessiné quelque chose ? »**, exactement le test `isDrawing` du front
(≥ 3 lignes portant un glyphe, ≥ 20 glyphes au total) — un seuil délibérément exigeant,
pour qu'un échantillon shell avec une flèche égarée ne passe pas pour un dessin.

⚠️ **Warning, jamais un refus** : ADR 0014/0035 — les checks croisés d'une écriture de
procédure signalent la dérive, ils ne la bloquent pas. ~14 procédures vivantes n'ont
aucun dessin ; les refuser casserait toute réécriture d'une procédure existante, et le
premier effet d'une garde bloquante serait qu'on cesse d'écrire des procédures.

⚠️ **Seuls les blocs NON TAGUÉS comptent** : c'est le routeur du front qui en décide
(`process-code-block.tsx`) — un ```text plein de caractères de tracé ne sera jamais
dessiné, donc le compter comme un dessin serait un faux positif silencieux, la classe
de bug que ce module existe pour fermer.
"""
from __future__ import annotations

import re

# L'alphabet du dessin, tel que la spec le nomme : les caractères de tracé de boîtes
# (U+2500–U+257F) plus les trois marqueurs de flux. Sous-ensemble strict de la classe
# du front (`DRAWING_GLYPH` dans `drawing.ts`, qui couvre aussi blocs et flèches) :
# ce qui passe ici passe donc là-bas, jamais l'inverse.
GLYPHS = re.compile(r"[─-╿▼▶▪]")

# Les deux seuils du front, à l'identique. Les changer ici sans les changer là-bas
# rendrait le warning menteur dans un sens ou dans l'autre.
MIN_GLYPH_LINES = 3
MIN_GLYPHS = 20

# Un bloc fencé : la ligne d'ouverture avec son langage optionnel, puis tout jusqu'à
# la fermeture — cherchée en début de ligne, pour qu'un ``` dans la prose ne close pas
# un bloc trop tôt. Même forme que `FENCE` dans `drawing.ts`.
_FENCE = re.compile(r"^[ \t]*```[ \t]*([\w-]*)[^\n]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)

WARNING = ("no flowchart found — add the drawing described in the procedure guide "
           "(procedure-flowchart)")

# Deux blocs qui dessinent : la page n'en rend qu'UN, le premier. Le cas se fabrique
# quand un corps arrive avec le marqueur ET un vrai dessin — `avec_le_dessin` remet
# alors le tracé stocké à côté du neuf, et `has_diagram` seul répondait « oui, il y a
# un dessin », donc rien n'était dit.
DOUBLE = ("two drawings in this body — the process page renders the FIRST one only. "
          "If you drew a new flowchart, DROP the `<!-- flowchart: … -->` marker line: "
          "kept, it put the stored drawing back next to yours")


def is_drawing(block: str) -> bool:
    """Ce bloc est-il un dessin plutôt qu'un échantillon de code ? Port du `isDrawing`
    du front : assez de glyphes, sur assez de lignes, pour que ce soit une structure et
    pas de la ponctuation."""
    lines_with = 0
    total = 0
    for line in block.split("\n"):
        found = len(GLYPHS.findall(line))
        if found:
            lines_with += 1
        total += found
    return lines_with >= MIN_GLYPH_LINES and total >= MIN_GLYPHS


def compter_les_dessins(body_md: str) -> int:
    """Combien de blocs NON TAGUÉS dessinent dans ce corps. La page n'en rend qu'un —
    le premier — donc au-delà de 1, un tracé est écrit et jamais montré."""
    return sum(1 for lang, block in _FENCE.findall(body_md or "")
               if not lang and is_drawing(block))


def has_diagram(body_md: str) -> bool:
    """Le corps porte-t-il un dessin que la page du process saura rendre ?"""
    return compter_les_dessins(body_md) > 0


def trouver_le_dessin(body_md: str) -> tuple[int, int] | None:
    """L'étendue `(début, fin)` du PREMIER bloc fencé non tagué qui dessine — fences
    comprises — ou `None`. Le même bloc que `has_diagram` compte, au même critère."""
    for m in _FENCE.finditer(body_md or ""):
        if not m.group(1) and is_drawing(m.group(2)):
            return m.span()
    return None


# ── Le dessin servi à l'agent : un marqueur, pas le tracé ─────────────────────
#
# Le dessin est la vue par défaut de la PAGE — un humain le regarde. L'agent qui
# déroule la procédure lit les étapes, qui disent le même flux en prose ; le tracé lui
# coûte ~1 000 jetons par lecture (mesuré le 10/09/2026 : 3 304 caractères = 983 jetons
# Haiku, et une procédure est lue à chaque run) et ne lui apprend rien qu'il n'ait
# déjà. Sur la face MCP, `op=get` remplace donc le bloc par UNE ligne.
#
# ⚠️ Cette ligne est un MARQUEUR, pas un commentaire : l'agent qui édite relit puis
# réécrit (`op=get` → `op=set`), et sans elle chaque édition d'agent ferait DISPARAÎTRE
# le dessin — la page se rendrait vide. À l'écriture, `avec_le_dessin` remet à sa place
# le tracé de la version courante. Un corps qui arrive avec un VRAI dessin le garde ;
# un corps sans marqueur ni dessin est ce qu'il a toujours été : `diagram_warning`.
# Les corps STOCKÉS ne portent jamais le marqueur — il ne vit qu'entre les deux appels.
#
# ⚠️ Ce que le marqueur NE promet pas : `avec_le_dessin` relit le corps COURANT de la
# ligne visée par l'écriture. Une écriture qui vise ailleurs n'a rien à relire, et le
# marqueur s'efface : `op=create`, un slug neuf, ou un `scope` omis après avoir lu
# l'org (le défaut d'écriture est `user`, ADR 0068). C'est pour ça que la ligne dit
# désormais « same slug and scope » plutôt que « op=set keeps the drawing ».
# ⚠️ Et marqueur + VRAI dessin dans le même corps donne DEUX blocs dessinants : la
# page n'en rend qu'un, le premier. `has_diagram` répondait « oui » et se taisait ;
# `compter_les_dessins` le compte, et `diagram_check` le DIT (`DOUBLE`).
_MARQUEUR = re.compile(r"^[ \t]*<!--[ \t]*flowchart:[^\n]*?-->[ \t]*$", re.M)


def marqueur(version, lignes: int) -> str:
    """La ligne qui remplace le tracé — et qui dit à QUELLES conditions il revient.

    ⚠️ Elle a d'abord promis « keep this line and op=set keeps the drawing », sans
    condition. C'était faux dans quatre cas mesurés le 10/09/2026, tous des écritures
    qui ne visent pas la ligne d'où le marqueur vient : `op=create` (rien à relire),
    un slug neuf, un `scope` omis (le défaut d'écriture est `user` — on relit l'org et
    on écrit chez soi, ADR 0068), et le marqueur simplement non recopié. Le dessin est
    alors perdu — pas en silence (`diagram_warning` le dit), mais après coup.

    Un texte servi est du code de prod : une promesse sans condition sera crue."""
    return (f"<!-- flowchart: v{version}, {lignes} lines, omitted here — keep this line "
            "VERBATIM; op=set puts the drawing back only on the SAME slug and scope you "
            "read (no scope = user, not org). op=create never does. "
            "op=get full=true reads it -->")


def sans_le_dessin(body_md: str, version) -> str:
    """Le corps avec son dessin remplacé par le marqueur ; intact s'il n'en a pas."""
    etendue = trouver_le_dessin(body_md)
    if etendue is None:
        return body_md
    debut, fin = etendue
    lignes = body_md[debut:fin].count("\n") + 1
    return body_md[:debut] + marqueur(version, lignes) + body_md[fin:]


def porte_le_marqueur(body_md: str) -> bool:
    return bool(_MARQUEUR.search(body_md or ""))


def avec_le_dessin(body_md: str, courant_md: str) -> str:
    """Le corps à ÉCRIRE : chaque marqueur remplacé par le dessin du corps courant.
    Sans dessin courant (création, ou une procédure qui n'en a jamais eu), le marqueur
    s'efface — et le corps tombe sous `diagram_warning`, comme tout corps sans dessin."""
    if not porte_le_marqueur(body_md):
        return body_md
    etendue = trouver_le_dessin(courant_md)
    dessin = courant_md[etendue[0]:etendue[1]] if etendue else ""
    # `\g<0>`-style replacement is not needed: the marker line is replaced whole.
    return _MARQUEUR.sub(lambda _m: dessin, body_md)


def diagram_check(body_md: str) -> dict:
    """Check croisé à l'écriture, dans la forme des autres (`slots_check`,
    `write_check`) : la clé est TOUJOURS présente, `None` = le check a tourné et
    n'a rien trouvé à dire. Best-effort — un check ne casse jamais une écriture."""
    try:
        combien = compter_les_dessins(body_md)
        return {"diagram_warning": WARNING if combien == 0
                else DOUBLE if combien > 1 else None}
    # noqa: SILENT — contrôle de forme optionnel : pas d'avertissement plutôt qu'un faux
    except Exception:  # noqa: BLE001 — cf. `slots_check`
        return {"diagram_warning": None}
