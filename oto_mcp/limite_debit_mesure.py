"""La MESURE de la limite de débit (`limite_debit`) : de quoi régler ses bornes sur le
trafic réel, sans jamais écrire par requête.

**Pourquoi.** Une borne se règle sur ce que font les consommateurs réels, pas sur un
idéal (`routes_lourdes`, #1145) — et en mode `report`, le compteur « aurait refusé »
est la SEULE trace de ce que ferait `enforce`. Sans lui, passer en `enforce` serait un
pari.

**Ce qui est compté**, par classe et par minute murale, en mémoire :

- `allowed` — décisions qui passent ;
- `would_refuse` — refus en mode `report` (laissés passer) ;
- `refused` — refus servis en 429 (mode `enforce`) ;
- `peak_pct` — le plus haut usage d'un seau dans la minute, toutes clés confondues, en
  % de sa rafale (100 = une clé est à sec). C'est le chiffre qui dit à quelle distance
  de la borne le client le plus actif est passé, sans nommer personne.

**Ce qui est écrit** : AU PLUS une ligne par classe et par minute, dans le journal
d'appels (`tool_calls`, `kind='debit'`, `tool='debit:<classe>'`, les compteurs dans
`args`, l'environnement comme pour `kind='transport'`) — jamais par requête, jamais une
adresse ni un jeton. L'écriture part en tâche de fond quand la minute change, hors du
chemin de réponse : un refus n'attend jamais la base, et un échec d'écriture ne casse
rien (il le dit). La minute en cours au moment d'un arrêt est perdue : c'est une mesure,
pas une comptabilité.

**Où se lit le résultat** : `oto_admin_monitoring op=transport` (REST
`GET /api/admin/monitoring/transport`), champ `rate_limit`, ventilé par environnement,
classe et mode.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_EN_VOL: set = set()


def _environnement() -> str:
    """Même axe que `transport_refusals` : préproduction et production écrivent dans
    la MÊME base, sans lui les deux trafics s'additionneraient en silence."""
    return (os.environ.get("OTO_SENTRY_ENV", "").strip().lower() or "inconnu")


class Mesure:
    """Compteurs d'une minute murale, par classe. `noter_*` ne fait que des additions
    en mémoire ; `basculer` rend les lignes de la minute close (ou rien) et repart."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.minute: int | None = None
        self.compteurs: dict[str, dict] = {}

    def _classe(self, classe: str) -> dict:
        return self.compteurs.setdefault(
            classe, {"allowed": 0, "would_refuse": 0, "refused": 0, "peak_pct": 0.0})

    def noter_decision(self, classe: str, issue: str) -> None:
        """`issue` ∈ {allowed, would_refuse, refused}."""
        self._classe(classe)[issue] += 1

    def noter_usage(self, classe: str, pct: float) -> None:
        c = self._classe(classe)
        if pct > c["peak_pct"]:
            c["peak_pct"] = pct

    def basculer(self, minute: int, bornes: dict) -> list[dict]:
        """Ferme la minute courante si `minute` en est une autre : rend ses lignes (une
        par classe active) et remet les compteurs à zéro. La première minute vue ne
        rend rien."""
        if self.minute == minute:
            return []
        lignes = []
        if self.minute is not None:
            lignes = [self._ligne(classe, c, bornes.get(classe))
                      for classe, c in sorted(self.compteurs.items())]
        self.minute, self.compteurs = minute, {}
        return lignes

    def _ligne(self, classe: str, c: dict, par_minute) -> dict:
        from .calllog import truncated_args

        debut = datetime.fromtimestamp(self.minute * 60, tz=timezone.utc)
        brut = {"env": _environnement(), "mode": self.mode,
                "minute": debut.isoformat(), "allowed": c["allowed"],
                "would_refuse": c["would_refuse"], "refused": c["refused"],
                "peak_pct": round(c["peak_pct"], 1),
                "limit_per_minute": int(par_minute) if par_minute else None}
        return {
            "server": "oto",
            "kind": "debit",
            "tool": f"debit:{classe}",
            # La fabrique commune, appelée ICI : c'est elle qui porte la promesse de
            # masquage (même discipline que `transport_refusals._ligne`).
            "args": truncated_args(brut) or {},
            "ok": not (c["would_refuse"] or c["refused"]),
            # Aucun `sub` : la décision précède toute identité.
        }


def _ecrire(lignes: list[dict]) -> None:
    from . import db
    for ligne in lignes:
        db.insert_tool_call(ligne)


async def _emettre(lignes: list[dict]) -> None:
    try:
        await asyncio.to_thread(_ecrire, lignes)
    except Exception:  # noqa: BLE001 — la mesure ne casse jamais le service
        logger.warning("écriture de la mesure du débit en échec (%d lignes)",
                       len(lignes), exc_info=True)


def publier(lignes: list[dict]) -> None:
    """Écrit les lignes d'une minute close, en tâche de fond. Hors boucle (tests), en
    direct — même chemin."""
    if not lignes:
        return
    try:
        boucle = asyncio.get_running_loop()
    except RuntimeError:
        try:
            _ecrire(lignes)
        except Exception:  # noqa: BLE001
            logger.warning("écriture de la mesure du débit en échec (%d lignes)",
                           len(lignes), exc_info=True)
        return
    tache = boucle.create_task(_emettre(lignes))
    _EN_VOL.add(tache)
    tache.add_done_callback(_EN_VOL.discard)
