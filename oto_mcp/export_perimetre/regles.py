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
  `'<org_id>:<sub>'` (`credentials_store.member_id`), `tenant` par son slug.
  `platform` ne relève JAMAIS d'un périmètre : ce sont les lignes de l'instance ;
- l'héritage d'un parent (`Via`), par une clé étrangère déclarée ou LOGIQUE
  (polymorphe, sans FK possible) — la seconde se déclare avec `fk=False` ;
- l'union (`Ou`), quand une table porte deux chemins (procédure d'org ou perso).

Une règle dit aussi, quand elle le sait, à quel COMPTE la ligne appartient (`comptes`,
des `Compte`) : la colonne de `ParSubSansOrg` (une ligne sans org est celle de son
compte, donc toute ligne l'est), le couple polymorphe de `ParEntite` quand il désigne
un `user` ou un `member`. Une ligne possédée par org peut porter le compte d'un ANCIEN
membre : `comptes` dit comment la rattacher ou l'omettre (`export_perimetre.comptes`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union


@dataclass(frozen=True)
class Compte:
    """Une colonne-COMPTE : elle dit à quel compte la ligne appartient.

    `valeur` est l'expression SQL du compte désigné (NULL si la ligne n'en désigne
    aucun) ; `remplacer(x)` celle de la colonne `colonne` où ce compte devient
    l'expression `x` — ce qui permet de comparer une ligne rattachée aux clés de son
    jumeau. `simple` : la colonne EST le compte (et peut porter une clé étrangère
    vers `users(sub)`)."""
    colonne: str
    valeur: str
    type_: str | None = None     # colonne du type, pour un couple polymorphe

    @property
    def simple(self) -> bool:
        return self.type_ is None

    @property
    def nom(self) -> str:
        return self.colonne if self.simple else f"{self.type_}/{self.colonne}"

    def remplacer(self, x: str) -> str:
        if self.simple:
            return x
        t, i = self.type_, self.colonne
        return (f"CASE {t} WHEN 'user' THEN {x} "
                f"WHEN 'member' THEN split_part({i}, ':', 1) || ':' || {x} ELSE {i} END")

    @classmethod
    def colonne_simple(cls, colonne: str) -> "Compte":
        return cls(colonne, colonne)

    @classmethod
    def polymorphe(cls, colonne_type: str, colonne_id: str) -> "Compte":
        """`user` par son sub, `member` en `'<org_id>:<sub>'` (le sub peut lui-même
        porter un `:` — celui d'un tenant tiers — d'où `strpos`, pas `split_part`)."""
        t, i = colonne_type, colonne_id
        return cls(i, f"(CASE {t} WHEN 'user' THEN {i} "
                      f"WHEN 'member' THEN substr({i}, strpos({i}, ':') + 1) END)", t)


@dataclass(frozen=True)
class ParOrg:
    colonne: str = "org_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(orgs)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)

    def comptes(self) -> tuple[Compte, ...]:
        return ()


@dataclass(frozen=True)
class ParSub:
    colonne: str = "sub"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(subs)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)

    def comptes(self) -> tuple[Compte, ...]:
        return ()


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

    def comptes(self) -> tuple[Compte, ...]:
        return (Compte.colonne_simple(self.colonne),)


@dataclass(frozen=True)
class ParGroupe:
    colonne: str = "group_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%(groupes)s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)

    def comptes(self) -> tuple[Compte, ...]:
        return ()


@dataclass(frozen=True)
class ParTenant:
    """Le tenant du périmètre, par son id (`colonne="id"` sur `tenants`) ou son slug."""
    colonne: str = "id"
    par_slug: bool = False

    def predicat(self, _parent: Callable[[str], str]) -> str:
        return f"{self.colonne} = ANY(%({'tenants_slug' if self.par_slug else 'tenants'})s)"

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne,)

    def comptes(self) -> tuple[Compte, ...]:
        return ()


@dataclass(frozen=True)
class ParEntite:
    """Le couple polymorphe (type, id). `tenant` s'y lit par son SLUG ; `platform`
    n'y entre jamais."""
    colonne_type: str = "owner_type"
    colonne_id: str = "owner_id"

    def predicat(self, _parent: Callable[[str], str]) -> str:
        t, i = self.colonne_type, self.colonne_id
        return (f"(({t} = 'org' AND {i} = ANY(%(orgs_txt)s))"
                f" OR ({t} = 'group' AND {i} = ANY(%(groupes_txt)s))"
                f" OR ({t} = 'user' AND {i} = ANY(%(subs)s))"
                f" OR ({t} = 'member' AND split_part({i}, ':', 1) = ANY(%(orgs_txt)s))"
                f" OR ({t} = 'tenant' AND {i} = ANY(%(tenants_slug)s)))")

    def colonnes(self) -> tuple[str, ...]:
        return (self.colonne_type, self.colonne_id)

    def comptes(self) -> tuple[Compte, ...]:
        return (Compte.polymorphe(self.colonne_type, self.colonne_id),)


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

    def comptes(self) -> tuple[Compte, ...]:
        return ()


@dataclass(frozen=True)
class Ou:
    regles: tuple["Regle", ...]

    def predicat(self, parent: Callable[[str], str]) -> str:
        return "(" + " OR ".join(f"({r.predicat(parent)})" for r in self.regles) + ")"

    def colonnes(self) -> tuple[str, ...]:
        return tuple(c for r in self.regles for c in r.colonnes())

    def comptes(self) -> tuple[Compte, ...]:
        return tuple(k for r in self.regles for k in r.comptes())


Regle = Union[ParOrg, ParSub, ParSubSansOrg, ParGroupe, ParTenant, ParEntite, Via, Ou]


def vias(regle: Regle) -> tuple[Via, ...]:
    """Les liens vers un parent qu'une règle emprunte, `Ou` déplié."""
    if isinstance(regle, Via):
        return (regle,)
    if isinstance(regle, Ou):
        return tuple(v for r in regle.regles for v in vias(r))
    return ()
