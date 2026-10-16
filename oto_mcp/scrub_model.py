"""Le modèle qui DÉSIGNE les passages sensibles d'un texte libre (action `scrub`).

Les formes (NIR, IBAN, carte, e-mail, téléphone, secret annoncé) se trouvent en local,
dans oto-core. Ce qu'une lecture seule trouve — un nom, une adresse, un identifiant dit
dans une phrase (`MODEL_KINDS`) — se demande à un modèle servi derrière un point d'accès
compatible OpenAI, que l'instance DÉCLARE par trois variables, ensemble ou pas du tout :
`OTO_SCRUB_MODEL_URL`, `OTO_SCRUB_MODEL`, `OTO_SCRUB_MODEL_KEY` (secret). Le modèle ne
réécrit jamais le texte : il désigne, oto-core remplace (`docs/redaction.md`).

Sans modèle déclaré, une règle qui en demande un est refusée à la pose
(`scrub_model_unconfigured`) ; une politique déjà posée retient la sortie — jamais une
passe réduite aux formes qui laisserait croire le texte nettoyé.
"""
from __future__ import annotations

import os
from typing import Optional

_VARIABLES = ("OTO_SCRUB_MODEL_URL", "OTO_SCRUB_MODEL", "OTO_SCRUB_MODEL_KEY")


def _valeurs() -> tuple[Optional[str], Optional[str], Optional[str]]:
    return (os.environ.get("OTO_SCRUB_MODEL_URL") or None,
            os.environ.get("OTO_SCRUB_MODEL") or None,
            os.environ.get("OTO_SCRUB_MODEL_KEY") or None)


def declare() -> bool:
    """L'instance a-t-elle déclaré son modèle ? Une déclaration partielle lève."""
    valeurs = _valeurs()
    if not any(valeurs):
        return False
    if not all(valeurs):
        manquent = [n for n, v in zip(_VARIABLES, valeurs) if not v]
        raise RuntimeError(f"modèle de rédaction déclaré à moitié : il manque {', '.join(manquent)} "
                           f"({', '.join(_VARIABLES)} vont ensemble)")
    return True


def detecteur():
    """Le détecteur du modèle déclaré, ou None si l'instance n'en déclare pas."""
    if not declare():
        return None
    from oto.tools.common.scrub import ChatSpanDetector

    url, modele, cle = _valeurs()
    return ChatSpanDetector(url, cle, modele)


def sortes_de_modele(regles: list[dict]) -> list[str]:
    """Les sortes que seules les règles `scrub` de `regles` demandent au modèle."""
    from oto.tools.common.scrub import MODEL_KINDS

    return sorted({k for r in regles or [] if r.get("action") == "scrub"
                   for k in (r.get("detect") or []) if k in MODEL_KINDS})
