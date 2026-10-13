"""Le PALMARÈS des refus : QUI les limiteurs auraient coupé, par jour (#1232, lot 1).

**Pourquoi.** Les deux limiteurs tournent en mode mesure (`report`) : la limite de débit
applicative (`limite_debit`, F-025) et l'équité d'une clé plateforme
(`access/platform_rate`, F-051). Leur mesure dit COMBIEN de décisions `enforce`
refuserait, pas QUI : une borne qui ne couperait qu'un seul client en boucle ne se lit
pas comme une borne qui couperait vingt clients ordinaires. Avant de rien refuser
(décision du 13/10/2026 : mesurer d'abord), il faut pouvoir dire, au bout d'une
semaine, qui aurait été coupé, combien de fois, par jour et par limiteur.

**Ce qui est compté** : chaque décision qu'`enforce` refuse ou refuserait, par
(limiteur, classe, clé HACHÉE), en mémoire, par minute murale :

- `debit` — la classe du seau qui refuse (`jeton`, `anonyme`, `sensible`, `webhook`,
  `webhook_adresse`, `echecs`) ; la clé est l'empreinte du jeton porté (la MÊME que
  `limite_debit._empreinte`, celle des lignes de journal `jeton abcd1234…`), ou
  l'empreinte de l'adresse (de « adresse chemin » pour `webhook` : le chemin d'un
  crochet porte un secret) ;
- `platform_rate` — la classe est le connecteur porteur de la clé plateforme ; la clé
  est l'empreinte du porteur `org:<id>` ou `user:<sub>`.

Jamais un jeton, une adresse, un e-mail ni un identifiant en clair : une empreinte se
VÉRIFIE (on calcule celle d'un candidat, `empreinte("org:42")`), elle ne se lit pas.

**Borné.** Au plus `MAX_CLES` clés distinctes par limiteur et par minute ; au-delà, les
refus vont à la ligne `DEBORDEMENT` (`*`) de leur classe — le total reste juste, seul
le nom se perd. Un refus suppose qu'une clé a vidé son seau : les clés refusées sont
par nature peu nombreuses.

**Jamais sur le chemin d'une requête.** `noter` ne fait qu'une addition en mémoire
sous un verrou ; la minute close part en UNE écriture agrégée (un upsert par clé, une
transaction) sur un fil d'écriture unique (démon), hors de la boucle et hors du fil de
la requête, par une file BORNÉE (`FILE_MAX` minutes en attente ; pleine, la minute est
perdue et le journal le dit). Un échec d'écriture se journalise et ne casse rien. Ce
qui n'est pas écrit à l'arrêt du processus est perdu : c'est une mesure, pas une
comptabilité — et un arrêt n'attend jamais la base.

**Rétention** : `RETENTION_JOURS`. Le fil d'écriture purge les jours plus anciens, une
fois par jour et par processus.

**Où se lit le résultat** : `oto_admin_monitoring op=transport` (REST
`GET /api/admin/monitoring/transport`), champ `rate_limit_top` — les `TOP_N` premières
clés par jour, environnement et limiteur.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEBIT, PLATEFORME = "debit", "platform_rate"
#: Clés distinctes retenues par limiteur et par minute ; au-delà, `DEBORDEMENT`.
MAX_CLES = 1000
#: La clé qui reçoit les refus des clés au-delà de `MAX_CLES` (jamais une empreinte).
DEBORDEMENT = "*"
RETENTION_JOURS = 30
#: Minutes closes en attente d'écriture, au plus (par processus).
FILE_MAX = 64
TOP_N = 10


def empreinte(valeur: str) -> str:
    """L'empreinte d'une clé : sha256 tronqué, comme `limite_debit._empreinte`."""
    return hashlib.sha256(valeur.encode()).hexdigest()[:32]


class Palmares:
    """Les refus d'un limiteur pendant une minute murale, par (classe, clé). Sûr entre
    fils : `platform_rate` compte depuis le fil d'une requête, `limite_debit` depuis la
    boucle."""

    def __init__(self, limiteur: str, max_cles: int = MAX_CLES) -> None:
        self.limiteur = limiteur
        self.max_cles = max_cles
        self.minute: Optional[int] = None
        self.compteurs: dict[tuple[str, str], list] = {}
        self._verrou = threading.Lock()

    def noter(self, classe: str, cle: str, mode: str, maintenant: float) -> list[dict]:
        """Compte un refus ; rend les lignes de la minute close si celle-ci en change
        (à publier par l'appelant, hors du verrou)."""
        with self._verrou:
            lignes = self._basculer(int(maintenant // 60))
            compteur = self.compteurs.get((classe, cle))
            if compteur is None:
                if len(self.compteurs) >= self.max_cles:
                    cle = DEBORDEMENT
                compteur = self.compteurs.setdefault(
                    (classe, cle), [0, maintenant, maintenant, mode])
            compteur[0] += 1
            compteur[2], compteur[3] = maintenant, mode
        return lignes

    def basculer(self, maintenant: float) -> list[dict]:
        """Rend les lignes de la minute close si `maintenant` est dans une autre (rien
        sinon) — appelé à chaque requête par `limite_debit`, d'où le chemin sans
        verrou quand la minute n'a pas changé."""
        minute = int(maintenant // 60)
        if self.minute == minute:
            return []
        with self._verrou:
            return self._basculer(minute)

    def _basculer(self, minute: int) -> list[dict]:
        if self.minute == minute:
            return []
        lignes = []
        if self.compteurs:
            from .limite_debit_mesure import _environnement
            env = _environnement()
            jour = datetime.fromtimestamp(self.minute * 60, tz=timezone.utc).date()
            lignes = [{"jour": jour, "env": env, "limiteur": self.limiteur,
                       "classe": classe, "cle": cle, "refus": n, "mode": mode,
                       "premier_at": datetime.fromtimestamp(premier, tz=timezone.utc),
                       "dernier_at": datetime.fromtimestamp(dernier, tz=timezone.utc)}
                      for (classe, cle), (n, premier, dernier, mode)
                      in sorted(self.compteurs.items())]
        self.minute, self.compteurs = minute, {}
        return lignes


#: Le palmarès de la clé plateforme : un par processus (le compteur F-051 est en base,
#: le palmarès des refus en mémoire, comme la mesure du débit).
PALMARES_PLATEFORME = Palmares(PLATEFORME)

_FILE: queue.Queue = queue.Queue(maxsize=FILE_MAX)
_FIL: Optional[threading.Thread] = None
_FIL_VERROU = threading.Lock()
_DERNIERE_PURGE: Optional[date] = None


def _boucle_d_ecriture() -> None:
    while True:
        lignes = _FILE.get()
        try:
            _ecrire(lignes)
        finally:
            _FILE.task_done()


def _demarrer_fil() -> None:
    """UN fil d'écriture par processus, démon : les minutes closes s'écrivent l'une
    après l'autre, et l'arrêt du processus ne l'attend pas."""
    global _FIL
    with _FIL_VERROU:
        if _FIL is None or not _FIL.is_alive():
            _FIL = threading.Thread(target=_boucle_d_ecriture, name="oto-refus-top",
                                    daemon=True)
            _FIL.start()


def _ecrire(lignes: list[dict]) -> None:
    global _DERNIERE_PURGE
    from .db import limite_refus_jour
    try:
        limite_refus_jour.ajouter(lignes)
        aujourdhui = datetime.now(timezone.utc).date()
        if _DERNIERE_PURGE != aujourdhui:
            limite_refus_jour.purger(RETENTION_JOURS)
            _DERNIERE_PURGE = aujourdhui
    except Exception:  # noqa: BLE001 — la mesure ne casse jamais le service
        logger.warning("écriture du palmarès des refus en échec (%d lignes)",
                       len(lignes), exc_info=True)


def publier(lignes: list[dict]) -> None:
    """Confie les lignes d'une minute close au fil d'écriture : ne bloque jamais
    l'appelant (boucle ou fil d'une requête)."""
    if not lignes:
        return
    _demarrer_fil()
    try:
        _FILE.put_nowait(lignes)
    except queue.Full:
        logger.warning("palmarès des refus : file d'écriture pleine, minute perdue "
                       "(%d lignes)", len(lignes))


def noter_plateforme(connecteur: str, porteur: str, mode: str,
                     maintenant: float) -> None:
    """Un appel qu'`enforce` refuse ou refuserait sur la clé plateforme `connecteur`,
    pour `porteur` (`org:<id>` | `user:<sub>`, haché ici)."""
    publier(PALMARES_PLATEFORME.noter(connecteur, empreinte(porteur), mode, maintenant))
