"""Les règles d'appartenance : quelles lignes d'une table relèvent du périmètre.

Une règle se compile en PRÉDICAT SQL sur la table, paramétré par le périmètre
(`Perimetre.parametres()`) : `%(orgs)s`, `%(orgs_txt)s`, `%(groupes)s`,
`%(groupes_txt)s`, `%(subs)s`. Elle nomme aussi les colonnes qu'elle lit, pour que
`decouverte.verifier_classement` refuse une règle qui vise une colonne disparue —
sinon un renommage de colonne ferait tomber la règle en erreur SQL au jour de
l'export, ou pire, la viderait.

Quatre formes de propriété coexistent dans le schéma, et chacune a sa règle :
- une colonne d'org (`ParOrg`), de compte (`ParSub`) ou d'équipe (`ParGroupe`) ; une
  ligne dont l'org est NULLE appartient à son compte (`ParSubSansOrg`) ;
- le couple POLYMORPHE (`ParEntite`) : `owner_type`/`owner_id` en texte, au
  vocabulaire du coffre — `org` et `group` par id, `user` par sub, `member` en
  `'<org_id>:<sub>'` (`credentials_store.member_id`). `platform` et `tenant` ne
  relèvent JAMAIS d'un périmètre d'orgs : ce sont des lignes de l'instance ;
- l'héritage d'un parent (`Via`), par une clé étrangère déclarée ou LOGIQUE
  (polymorphe, sans FK possible) — la seconde se déclare avec `fk=False` ;
- l'union (`Ou`), quand une table porte deux chemins (procédure d'org ou perso).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union


@dataclass(frozen=True)
class ParOrg:
    colonne: str = "org_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(orgs)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)


@dataclass(frozen=True)
class ParSub:
    colonne: str = "sub"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(subs)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)


@dataclass(frozen=True)
class ParSubSansOrg:
    """Une ligne SANS org appartient à son compte (`org_id` NULL : un droit « personne,
    partout », un appel hors de toute org). Complète `ParOrg` par `Ou` — sans elle, ces
    lignes tomberaient hors du périmètre en silence."""
    colonne: str = "sub"
    colonne_org: str = "org_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"({self.colonne_org} IS NULL AND {self.colonne} = ANY(%(subs)s))"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne, self.colonne_org)


@dataclass(frozen=True)
class ParGroupe:
    colonne: str = "group_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(groupes)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)


@dataclass(frozen=True)
class ParEntite:
    """Le couple polymorphe (type, id) — `platform`/`tenant` n'y entrent jamais."""
    colonne_type: str = "owner_type"
    colonne_id: str = "owner_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        t, i = self.colonne_type, self.colonne_id
        return (f"(({t} = 'org' AND {i} = ANY(%(orgs_txt)s))"
                f" OR ({t} = 'group' AND {i} = ANY(%(groupes_txt)s))"
                f" OR ({t} = 'user' AND {i} = ANY(%(subs)s))"
                f" OR ({t} = 'member' AND split_part({i}, ':', 1) = ANY(%(orgs_txt)s)))")

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne_type, self.colonne_id)


@dataclass(frozen=True)
class Via:
    """La ligne appartient au périmètre si son PARENT y appartient.

    `fk=True` : une clé étrangère déclarée porte exactement ce lien, et la
    vérification l'exige dans le schéma réel. `fk=False` : lien LOGIQUE (id
    polymorphe en texte, table sans FK) — `quand` en donne le discriminant
    (`("kind", "brief")`) et `texte` dit que l'enfant porte l'id en TEXT.
    """
    parent: str
    colonnes_enfant: tuple[str, ...]
    colonnes_parent: tuple[str, ...] = ("id",)
    fk: bool = True
    texte: bool = False
    quand: tuple[str, str] | None = None

    def predicat(self, parent: Callable[[str], str]) -> str:
        cible = ", ".join(f"{c}::text" if self.texte else c for c in self.colonnes_parent)
        lien = (f"({', '.join(self.colonnes_enfant)}) IN "
                f"(SELECT {cible} FROM {self.parent} WHERE {parent(self.parent)})")
        if self.quand is None:
            return lien
        colonne, valeur = self.quand
        return f"({colonne} = '{valeur}' AND {lien})"

    def colonnes(self) -> tuple[str, ...]:
        return self.colonnes_enfant + ((self.quand[0],) if self.quand else ())


@dataclass(frozen=True)
class Ou:
    regles: tuple["Regle", ...]

    def predicat(self, parent: Callable[[str], str]) -> str:
        return "(" + " OR ".join(f"({r.predicat(parent)})" for r in self.regles) + ")"

    def colonnes(self) -> tuple[str, ...]:
        return tuple(c for r in self.regles for c in r.colonnes())


Regle = Union[ParOrg, ParSub, ParSubSansOrg, ParGroupe, ParEntite, Via, Ou]


def vias(regle: Regle) -> tuple[Via, ...]:
    """Les liens vers un parent qu'une règle emprunte, `Ou` déplié."""
    if isinstance(regle, Via):
        return (regle,)
    if isinstance(regle, Ou):
        return tuple(v for r in regle.regles for v in vias(r))
    return ()
