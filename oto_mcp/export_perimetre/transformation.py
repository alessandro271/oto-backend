"""La transformation d'une ligne source en ligne CIBLE — une seule fonction, deux usages.

L'export l'applique pour calculer l'AAD sous laquelle il rechiffre un secret (l'AAD
d'un credential de compte ou de membre contient le sub, qui change) ; l'import
l'applique pour écrire la ligne. Deux copies divergeraient en silence : le secret se
rechiffrerait sous une identité que l'import n'écrit pas, et deviendrait illisible.

Ce qu'elle change, et rien d'autre :
- **les comptes** perdent le préfixe `<slug>:` que leur donnait un tenant tiers
  (`Perimetre.comptes_cible`) : toute VALEUR exactement égale à un sub du périmètre,
  ou à sa forme membre `<org>:<sub>`, à toute profondeur d'un JSON ;
- **le tenant** devient la ligne 1 de la cible : sa ligne prend l'id 1, et toute clé
  étrangère vers `tenants(id)` vaut 1.
"""
from __future__ import annotations

from dataclasses import dataclass

from .decouverte import Schema


def _denuder(v, comptes: dict[str, str]):
    if isinstance(v, str):
        if v in comptes:
            return comptes[v]
        tete, sep, reste = v.partition(":")
        if sep and tete.isdigit() and reste in comptes:
            return f"{tete}:{comptes[reste]}"
        return v
    if isinstance(v, list):
        return [_denuder(x, comptes) for x in v]
    if isinstance(v, dict):
        return {k: _denuder(x, comptes) for k, x in v.items()}
    return v


@dataclass(frozen=True)
class Transformation:
    comptes: dict[str, str]                    # sub source → sub cible, identités retirées
    vers_tenant: dict[str, tuple[str, ...]]    # table → colonnes de clé vers `tenants(id)`

    @classmethod
    def depuis(cls, schema: Schema, comptes: dict[str, str]) -> "Transformation":
        return cls({s: c for s, c in comptes.items() if s != c},
                   {k.table: k.colonnes for k in schema.cles
                    if k.cible == "tenants" and k.colonnes_cible == ("id",)})

    def appliquer(self, table: str, ligne: dict) -> dict:
        cible = _denuder(ligne, self.comptes) if self.comptes else dict(ligne)
        for c in self.vers_tenant.get(table, ()):
            if cible[c] is not None:
                cible[c] = 1
        if table == "tenants":
            cible["id"] = 1
        return cible
