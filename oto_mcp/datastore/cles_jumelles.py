"""Deux orthographes du même mot dans un même schéma : la faute de frappe qu'aucune
table de correspondance ne peut voir.

L'avertissement des clés non interprétées (`cles_inconnues`, `vocabulaire`) compare ce
qu'un schéma déclare au vocabulaire d'oto, et propose une correction quand la clé morte
a une cousine VIVANTE (`enum` → `options`). Ce mécanisme est aveugle par construction
au cas ci-dessous, et c'est un consommateur qui l'a montré :

    raison_sociale   proprietaire     ← la clé voulue
    effectif         propietaire      ← une faute de frappe

Les deux sont libres : oto n'en interprète AUCUNE, donc aucune n'a de cousine dans le
vocabulaire, donc les deux ressortent avec `near_miss` vide, **côte à côte dans la même
phrase**, sans que rien ne distingue la clé délibérée de la coquille.

## Ce que coûte la coquille

Une clé libre n'est pas décorative : un consommateur la lit en aval — un écran client
groupe ses colonnes par `proprietaire`. La colonne qui porte `propietaire` n'est alors
**pas en erreur, elle est silencieusement absente** de ce regroupement. Rien ne casse,
rien n'alerte : un chiffre servi devient faux, et il a l'air juste.

C'est le même mécanisme que `read_only` au lieu de `readonly` (cas fondateur de
`cles_inconnues`), à une différence près qui le rend plus dur à voir : là-bas la clé
juste appartient à oto, donc la plateforme peut nommer la bonne. Ici **elle ne le peut
pas** — les deux formes lui sont également étrangères.

## L'axe : la COEXISTENCE, pas l'orthographe

La plateforme n'a aucun moyen de savoir que `proprietaire` s'écrit ainsi ; le demander
reviendrait à lui faire tenir le dictionnaire de tous ses consommateurs, et à refuser
demain une clé légitime qu'elle ne connaît pas encore. Ce qu'elle peut mesurer sans
rien interpréter, c'est que **deux clés libres à un caractère l'une de l'autre
coexistent dans le même schéma** — un fait mécanique, vrai ou faux, jamais une opinion
sur le métier.

## Seuil volontairement étroit

⚠️ Un faux positif dans un signal de qualité est pire que pas de signal : on apprend à
l'ignorer, et il ne sert plus le jour où il a raison (doctrine de `cles_inconnues`,
payée sur `label`). Deux formes seulement sont retenues, parce qu'elles ne se
choisissent jamais exprès :

- **une insertion ou une suppression** d'un caractère (`propietaire`) ;
- **une transposition** de deux caractères adjacents (`proprietiare`).

La **substitution** est délibérément EXCLUE, bien qu'elle soit une faute de frappe
courante : `label_fr` / `label_en`, `seuil_min` / `seuil_max`, `col_a` / `col_b` sont à
une substitution l'une de l'autre et parfaitement intentionnelles. Les signaler ferait
crier l'avertissement sur des schémas sains — exactement ce qui l'userait.

Longueur minimale de 5 caractères pour la même raison : sur des clés courtes, un
caractère d'écart est le mode NORMAL de distinction.

## Avertissement, jamais refus

Même régime que tout ce voisinage : la pose réussit. La plateforme ne sait pas qui lit
quoi en aval, et **les deux formes peuvent être délibérées** — un consommateur a le
droit d'avoir deux attributs proches. Le message dit ce qu'il a mesuré et laisse
trancher celui qui sait.
"""
from __future__ import annotations

from typing import Optional

#: En deçà, un caractère d'écart distingue normalement deux clés (`min`/`max`).
LONGUEUR_MINIMALE = 5


def _une_insertion(court: str, long: str) -> bool:
    """`long` s'obtient-il de `court` en insérant UN caractère ?"""
    i = 0
    for c in long:
        if i < len(court) and court[i] == c:
            i += 1
    return i == len(court)


def _une_transposition(a: str, b: str) -> bool:
    """`a` et `b` ne diffèrent-ils que par deux caractères adjacents échangés ?"""
    ecarts = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(ecarts) != 2:
        return False
    i, j = ecarts
    return j == i + 1 and a[i] == b[j] and a[j] == b[i]


def sont_jumelles(a: str, b: str) -> bool:
    """Deux clés distinctes que personne n'écrit exprès à un caractère près.

    ⚠️ Ni la substitution ni la casse : `label_fr`/`label_en` et `Champ`/`champ` sont
    des distinctions que des schémas sains utilisent. Voir le seuil, en tête de module.
    """
    if a == b or min(len(a), len(b)) < LONGUEUR_MINIMALE:
        return False
    if abs(len(a) - len(b)) == 1:
        return _une_insertion(*sorted((a, b), key=len))
    return len(a) == len(b) and _une_transposition(a, b)


def cles_libres_jumelles(inconnues: list[dict]) -> list[dict]:
    """Les paires de clés LIBRES presque identiques du schéma, avec leurs porteuses.

    Prend le relevé de `unknown_declaration_keys` — une seule dérivation pour les deux
    avertissements, comme partout ici : le jour où l'une des deux clés entre dans le
    vocabulaire lu, elle disparaît du relevé et ce signal s'éteint tout seul.

    Rend `[{"formes": [(clé, [champs…]), (clé, [champs…])]}]`, la forme la plus portée
    en premier — **sans jamais dire laquelle est juste** : le compte est un fait, la
    conclusion appartient à qui connaît le consommateur.
    """
    porteuses: dict[str, list[str]] = {}
    for e in inconnues or []:
        champ = e.get("field") or "?"
        for cle in e.get("keys") or []:
            porteuses.setdefault(cle, []).append(champ)

    cles = sorted(porteuses)
    paires = []
    for i, a in enumerate(cles):
        for b in cles[i + 1:]:
            if sont_jumelles(a, b):
                formes = sorted(((a, porteuses[a]), (b, porteuses[b])),
                                key=lambda f: (-len(f[1]), f[0]))
                paires.append({"formes": formes})
    return paires


def cles_jumelles_warning(paires: list[dict]) -> Optional[str]:
    """Le message servi — aux DEUX faces, à la pose comme à la lecture.

    ⚠️ Contrairement au voisinage, la même phrase sert l'auteur et le lecteur : elle ne
    pose aucune question (« vouliez-vous écrire… ? ») et ne désigne aucune autorité,
    parce qu'oto n'en connaît aucune ici. Elle ne rend qu'un fait et sa conséquence, et
    ces deux-là intéressent autant celui qui a écrit le schéma que celui qui le lit.
    """
    if not paires:
        return None

    dits = []
    for p in paires[:3]:
        (ka, ca), (kb, cb) = p["formes"]
        dits.append(f"`{ka}` ({len(ca)} colonne(s) : {', '.join(ca[:3])}) "
                    f"et `{kb}` ({len(cb)} colonne(s) : {', '.join(cb[:3])})")

    return ("⚠️ Deux orthographes très proches d'une même clé libre coexistent dans ce "
            "schéma : " + " ; ".join(dits) + ("…" if len(paires) > 3 else "") + ". "
            "oto n'interprète NI l'une NI l'autre et ne peut donc pas dire laquelle "
            "est la bonne — mais deux formes à un caractère près dans un même schéma "
            "sont presque toujours une coquille. ⚠️ Ce qu'elle coûte : le consommateur "
            "qui lit une de ces clés ne verra pas la colonne qui porte l'autre. Elle "
            "ne sera pas EN ERREUR, elle sera silencieusement ABSENTE — un décompte "
            "servi devient faux et garde l'air juste. Vérifie laquelle ton "
            "consommateur lit vraiment avant de corriger : les deux peuvent être "
            "délibérées, oto n'en sait rien.")
