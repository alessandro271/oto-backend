"""Documents légaux — métadonnées (version/label/url) DÉCLARÉES par l'instance.

Le contenu des docs vit sur le site de l'opérateur de l'instance ; ici on ne tient que
les MÉTADONNÉES (slug → version courante + libellé + URL) et la carte des CONTEXTES
(quels docs sont requis pour « accéder » vs « acheter »). Le backend `me.legal` en
dérive le reste-à-accepter ; la table `legal_acceptances` ne trace que le consentement.

**Déclarées, jamais écrites ici** (décision du 28/09/2026, oto-backend#968) : les
métadonnées vivaient en dur dans ce fichier, URL de NOTRE site comprises — toute autre
instance faisait donc accepter NOS contrats à ses utilisateurs. Elles viennent
désormais de `OTO_LEGAL_DOCS` (JSON, un objet par slug de `CONTEXTS`), sans défaut :
absente ou incomplète, `current_docs()` lève en nommant ce qui manque, et
`identite_instance.verifier` refuse le démarrage.

⚠️ **Le bump de version est un geste en deux endroits** : le texte publié sur le site
(`current` côté site) et la `version` déclarée dans l'environnement de l'instance.
Sans le second, un doc modifié ne redemande pas l'acceptation. Cet alignement a dérivé
une fois, huit jours durant (oto-websites#74) : les clients gardaient l'acceptation
d'une CGV périmée alors que le texte corrigé était en ligne. **Aucun banc ne peut
détecter cette dérive** — les deux vérités vivent hors de ce dépôt ; la garde est
humaine, et il faut le savoir plutôt que de croire le contraire.

**Un tenant tiers a ses PROPRES documents** — même besoin que
`orgs.front_base_url`/`front_brand` (invitations) ou `guides` scope `tenant` : `docs_for`
en est le seam. Un override par (tenant, slug) vit dans `tenant_legal_docs` (table, lue
en LIVE, sans redémarrage) ; absent, le slug garde la déclaration de l'instance.
"""
from __future__ import annotations

import json
from typing import Optional

from . import db, tenancy
from .config import require_env

_ENV = "OTO_LEGAL_DOCS"
_CHAMPS = ("version", "label", "url")


class DocumentsLegauxMalDeclares(RuntimeError):
    """`OTO_LEGAL_DOCS` est posée mais ne décrit pas les documents attendus."""


# Contexte → docs requis. `access` = à l'inscription (CGU) ; `purchase` = à l'achat.
# Un override de tenant ne peut pas AJOUTER de slug à un contexte — seulement
# remplacer version/label/url d'un slug qui y figure déjà.
CONTEXTS: dict[str, list[str]] = {
    "access": ["terms"],
    "purchase": ["terms", "cgv", "dpa"],
}


def slugs_attendus() -> tuple[str, ...]:
    """Chaque document qu'un contexte exige, une fois, dans l'ordre de `CONTEXTS`."""
    return tuple(dict.fromkeys(slug for slugs in CONTEXTS.values() for slug in slugs))


def current_docs() -> dict[str, dict[str, str]]:
    """Les métadonnées que l'instance DÉCLARE (`OTO_LEGAL_DOCS`), validées : exactement
    les slugs de `CONTEXTS`, chacun avec `version`, `label` et `url` non vides. Relue et
    revalidée à chaque appel — une variable d'environnement ne change pas en cours de
    process, mais un dict partagé muterait sous les pieds de l'appelant suivant.

    Lève `RuntimeError` si la variable manque, `DocumentsLegauxMalDeclares` si elle
    ne décrit pas les documents attendus — jamais un défaut à la place."""
    brut = require_env(_ENV)
    try:
        declares = json.loads(brut)
    except ValueError as exc:
        raise DocumentsLegauxMalDeclares(f"{_ENV} n'est pas du JSON lisible : {exc}") from exc
    if not isinstance(declares, dict):
        raise DocumentsLegauxMalDeclares(
            f"{_ENV} doit être un objet {{slug: {{version, label, url}}}}.")
    attendus = slugs_attendus()
    problemes = []
    if set(declares) != set(attendus):
        problemes.append(f"slugs déclarés {sorted(declares)}, attendus {sorted(attendus)}")
    for slug in attendus:
        meta = declares.get(slug)
        if not isinstance(meta, dict):
            continue
        vides = [c for c in _CHAMPS if not (isinstance(meta.get(c), str) and meta[c].strip())]
        if vides:
            problemes.append(f"{slug} : {', '.join(vides)} manquant(s) ou vide(s)")
    if problemes:
        raise DocumentsLegauxMalDeclares(
            f"{_ENV} ne décrit pas les documents légaux de l'instance — "
            + " ; ".join(problemes) + ".")
    return {slug: {c: declares[slug][c].strip() for c in _CHAMPS} for slug in attendus}


def docs_for(tenant_slug: str) -> dict[str, dict[str, str]]:
    """`current_docs()`, avec les overrides déclarés par `tenant_slug` fusionnés
    par-dessus, slug par slug. Chaque appel relit la table — c'est ce qui rend un
    override effectif sans redéploiement ni redémarrage du process.

    Le tenant PRIMAIRE court-circuite la lecture : la déclaration de l'instance EST
    la sienne, il n'y a jamais de ligne à chercher pour lui — et ça évite un aller PG
    sur le chemin le plus emprunté."""
    docs = current_docs()
    if not tenant_slug or tenant_slug == tenancy.primary_slug():
        return docs
    overrides = db.get_tenant_legal_docs(tenant_slug)
    if not overrides:
        return docs
    return {slug: {**meta, **overrides.get(slug, {})} for slug, meta in docs.items()}


# ── ce qui reste à accepter ──────────────────────────────────────────────────
#
# Un SEUL calcul du « reste à accepter », partagé par les deux gates qui s'en
# servent : le gate d'accès du dashboard (`capabilities/me_legal`) et le gate
# d'achat (`billing.subscribe`, #487). Deux calculs auraient divergé au premier
# document ajouté à un contexte — et la divergence se serait vue en production,
# sur un tunnel de paiement qui laisse passer ce que l'écran d'accès refuse.

def is_current(acceptances: dict, docs: dict, slug: str) -> bool:
    """Ce doc est-il accepté À LA VERSION COURANTE ? Une acceptation d'une version
    antérieure ne compte pas — c'est tout l'intérêt du bump de version."""
    accepte = acceptances.get(slug)
    return accepte is not None and accepte["version"] == docs[slug]["version"]


def missing_docs(acceptances: dict, docs: dict, slugs: list[str]) -> list[dict]:
    """Les documents de `slugs` qui restent à accepter, DÉCRITS assez pour être
    présentés à quelqu'un : slug, libellé, version courante, URL — et la version
    déjà acceptée s'il y en a une.

    `accepted_version` est ce qui distingue « jamais accepté » (None) de « accepté
    à une version périmée » : un refus qui ne dit pas lequel des deux enverrait
    l'utilisateur chercher une case qu'il a déjà cochée.

    Fonction PURE : les acceptations et les docs arrivent en argument, la lecture
    est à l'appelant (le statut du dashboard en fait UNE pour tous les contextes)."""
    manquants = []
    for slug in slugs:
        if is_current(acceptances, docs, slug):
            continue
        accepte = acceptances.get(slug)
        manquants.append({
            "slug": slug,
            "label": docs[slug]["label"],
            "version": docs[slug]["version"],
            "url": docs[slug]["url"],
            "accepted_version": accepte["version"] if accepte else None,
        })
    return manquants


def missing_for_sub(sub: Optional[str], context: str) -> list[dict]:
    """`missing_docs` pour CE sub et CE contexte — la version qui lit la base.

    Les documents sont ceux de SON tenant (un sub d'un tenant tiers doit SES CGU,
    pas celles d'oto) ; les acceptations sont les siennes, à la ligne la plus
    récente de chaque doc.

    `sub` absent ⟹ aucune acceptation ⟹ tout est dû : le gate se ferme, il ne
    s'ouvre pas."""
    required = CONTEXTS.get(context)
    if required is None:
        raise ValueError(f"unknown_context: contexte légal inconnu : {context!r}")
    docs = docs_for(tenancy.current().tenant_of(sub))
    acceptances = db.get_legal_acceptances(sub) if sub else {}
    return missing_docs(acceptances, docs, required)
