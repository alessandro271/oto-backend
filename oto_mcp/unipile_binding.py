"""Propriété d'un compte Unipile : la garde commune au point d'écriture."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db

@dataclass(frozen=True)
class BindOutcome:
    """Ce qu'une tentative de liaison a RÉELLEMENT fait.

    Pas un booléen : le refus a une CAUSE, et un appelant qui ne peut pas la lire ne
    peut pas la journaliser — un refus muet est un refus que personne ne saura avoir
    eu."""

    bound: bool
    reason: "str | None" = None


@dataclass(frozen=True)
class Provenance:
    """D'où vient le compte qu'on s'apprête à lier : ce qui peut prouver qu'il est à `sub`.

    Le fournisseur ne porte pas notre nonce sur le compte qu'il crée (hosted-auth v2) :
    il n'y a donc que deux preuves, et la garde les exige. **Une ligne du réclamant**
    (`a_moi`) : Unipile réutilise l'identifiant à la reconnexion, une ligne morte de `sub`
    prouve une propriété qui dure. **Sinon, la chronologie** : le compte a été créé chez le
    fournisseur (`cree_le`) APRÈS la demande de liaison de `sub` (`plancher`), à la marge
    d'horloge près. Une date absente ou illisible ne prouve rien — elle refuse."""

    a_moi: bool
    cree_le: "datetime | None"
    plancher: "datetime | None"


# Marge d'horloge entre notre base et le fournisseur.
MARGE_HORLOGE = timedelta(minutes=5)


def account_claimable(sub: str, account_id: str, provenance: Provenance, *,
                      foreign: "set | None" = None) -> bool:
    """Ce `account_id` est-il réclamable par `sub` ?

    **LA garde de toute liaison d'un compte de messagerie hébergée.** Née sur un seul
    des deux chemins qui écrivaient alors (#559) : la réconciliation contrôlait, le
    webhook de notification reprenait `body["account_id"]` tel quel. Le nonce prouve
    « c'est bien la session de connexion de cette personne » ; il ne dit rien de
    « c'est bien le compte qui vient d'être créé ». Et la clé fournisseur étant
    **partagée entre les organisations**, un identifiant quelconque de l'abonnement —
    le siège d'une autre org — était joignable sous cette liaison. Le webhook est
    retiré depuis (#581, 2026-08-29) ; la garde reste, au point d'écriture.

    Deux règles, les deux au point d'écriture :

    1. **Jamais le compte d'un autre.** Un identifiant déjà attribué à QUELQU'UN
       D'AUTRE, binding vivant ou mort, n'est pas réclamable. Une ligne morte d'un tiers
       vaut interdiction (Unipile réutilise le même identifiant à la reconnexion : elle
       prouve une propriété qui dure).
    2. **Jamais un siège orphelin (#580).** Un compte présent sur l'abonnement partagé et
       lié à PERSONNE chez nous n'est pas « libre » pour autant : sans preuve de
       `provenance` (une ligne du réclamant, ou une création postérieure à sa demande), il
       est refusé. Cette règle vivait dans la seule réconciliation, sous le nom de
       plancher de date, et y cédait quand une date était illisible ; elle vit ici,
       et une date illisible refuse.

    `foreign` : l'inventaire déjà chargé par un appelant qui teste N candidats (la
    réconciliation en voit tout l'abonnement). Absent, il est lu ici, au grain."""
    if not account_id:
        return False
    if foreign is not None:
        if account_id in foreign:
            return False
    elif db.is_foreign_unipile_account(sub, account_id):
        return False
    if provenance.a_moi:
        return True
    if provenance.cree_le is None or provenance.plancher is None:
        return False
    return provenance.cree_le >= provenance.plancher - MARGE_HORLOGE


def bind_account(sub: str, account_id: str, provenance: Provenance, *,
                 org_id: "int | None", provider: str = "LINKEDIN",
                 platform_seat: bool = False, account_name: "str | None" = None,
                 foreign: "set | None" = None) -> BindOutcome:
    """Écrire la liaison `(sub, org, canal) → account_id`, **gardée**.

    Le seul chemin d'écriture, et c'est le point : #559 n'était pas une garde oubliée
    mais une garde posée UNE fois sur DEUX écritures parallèles. Les mettre sous la
    même fonction est ce qui empêche une troisième de naître sans elle. Le webhook,
    second écrivain, est retiré (#581, 2026-08-29) ; la garde reste ICI, au point
    d'écriture, pour que le prochain chemin — un webhook v2 signé ? — naisse gardé."""
    if not account_claimable(sub, account_id, provenance, foreign=foreign):
        return BindOutcome(False, "account_not_claimable")
    db.set_unipile_account(sub, account_id, account_name=account_name,
                           org_id=org_id, provider=provider,
                           platform_seat=platform_seat)
    return BindOutcome(True)
