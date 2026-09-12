"""Le catalogue des modèles qu'un agent hébergé peut déclarer — et leur FAMILLE.

Un agent (déclencheur, flotte) nomme un modèle ; le travail qu'il produit emporte
ce modèle ET sa famille. La famille est ce qui ROUTE : un worker ne réserve que
les travaux d'une famille qu'il sait servir, c'est-à-dire du dépôt de clé qu'il
nomme au claim (`provider`, cf. `runner_jobs._cle_de_modele`). Les deux mots
coïncident à dessein — la famille EST le nom du dépôt.

⚠️ **Un travail SANS famille est servi par N'IMPORTE QUEL worker**, sur son propre
modèle — c'est le comportement d'avant ce catalogue, à l'octet près, et c'est ce
que portent tous les déclencheurs et toutes les flottes déclarés avant lui. Une
règle stricte (« pas de famille, pas de worker ») aurait orphelin chacun d'eux le
jour du déploiement, sans une erreur.

⚠️ **Le catalogue refuse à l'ÉCRITURE, jamais à la lecture.** Une valeur inconnue
déjà en base (une flotte déclarée avec un modèle libre avant ce lot) se lit
toujours ; elle ne voyage simplement pas avec le travail, puisque rien ne sait la
router.

Pur : aucune dépendance, pour que la base comme les capacités puissent le lire.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class Modele(NamedTuple):
    id: str
    label: str
    family: str
    default: bool = False


MODELES: tuple[Modele, ...] = (
    Modele("claude-sonnet-5", "Claude Sonnet 5", "anthropic", default=True),
    Modele("claude-opus-5", "Claude Opus 5", "anthropic"),
    Modele("claude-haiku-4-5", "Claude Haiku 4.5", "anthropic"),
    # La voie Conversations des workers de production (cf. oto-runner).
    Modele("mistral-large-2512", "Mistral Large", "mistral"),
)

_PAR_ID = {m.id: m for m in MODELES}

#: Les familles connues — c'est-à-dire les seuls dépôts dont la présence se note.
FAMILLES = frozenset(m.family for m in MODELES)


def famille(model: Optional[str]) -> Optional[str]:
    """La famille d'un modèle du catalogue, ou None (absent, ou inconnu)."""
    m = _PAR_ID.get(model or "")
    return m.family if m else None


def charge(model: Optional[str]) -> dict:
    """Ce qu'un travail emporte de son modèle : `{model, model_family}`, ou rien.

    ⚠️ Un modèle que le catalogue ne connaît pas ne part PAS : sans famille, il
    atteindrait un worker quelconque qui tenterait de l'appeler chez un
    fournisseur qui ne le sert peut-être pas. Ne rien envoyer rend le travail au
    comportement d'avant — le worker tourne sur son propre modèle."""
    f = famille(model)
    return {"model": model, "model_family": f} if f else {}


def catalogue(familles_servies) -> list[dict]:
    """Le catalogue tel qu'un écran le propose : chaque modèle, et s'il est SERVI
    — une famille dont un worker a sondé la file dans la fenêtre de présence."""
    servies = set(familles_servies or ())
    return [{"id": m.id, "label": m.label, "family": m.family,
             "default": m.default, "served": m.family in servies}
            for m in MODELES]
