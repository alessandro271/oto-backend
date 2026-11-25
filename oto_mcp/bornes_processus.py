"""Les BORNES GLOBALES du processus (#1232, lot 4) — uniformes, en mode mesure d'abord.

**Règle (13/10/2026).** Aucune protection par route ni par outil : une route lourde
ne se plafonne pas, on l'allège. Ce qui reste, ce sont quatre bornes qui valent pour
TOUT ce que sert le processus (et, depuis le 25/11/2026, deux réglages du transport
de l'SDK `mcp`, plus bas) :

- `corps` — la taille du corps d'une requête HTTP (`borne_corps`) : 4 Mo, 110 Mo sur les
  routes d'upload, reconnues à leur DÉCLARATION (`ContratDeRoute.corps` d'un type autre
  que JSON). En application : `413 payload_too_large` ;
- `outil` — la durée d'un appel d'outil MCP (`middleware/delai_outil`) : 120 s. En
  application : une erreur MCP `tool_timeout` qui dit le délai ;
- `sql` — le `statement_timeout` des connexions du pool applicatif (`db/borne_sql`) :
  30 s. En application : posé par connexion (`_conn._connect_options`) ;
- `session` — l'inactivité d'une session MCP (`borne_session`) : 30 min, une borne de
  l'SDK `mcp` (`session_idle_timeout`), sans aucun état par principal.
  **Exception au mode** : elle est armée dès que le mode n'est pas `off` (`report`
  compris) — ce n'est pas un refus (le client rouvre sa session sur le 404), c'est ce
  qui rend la mémoire des sessions que leurs clients ne ferment jamais.

**Le transport de l'SDK `mcp` (1.30, 25/11/2026)** — deux réglages posés dans TOUS les
modes (`borne_session.plafonner`), parce que l'SDK les applique de lui-même, quel que
soit notre interrupteur :

- `corps_mcp` — la taille max du corps d'une requête `/mcp` acceptée par l'SDK
  (`max_request_body_size`, 4 MiB par défaut chez lui → `413`) : 32 MiB ici, au-dessus
  du `request_body` du proxy (32 Mo). L'SDK ne refuse donc rien que le proxy laisse
  passer ; la borne `corps` ci-dessus (4 Mo, mesurée en `report`) reste celle qu'on
  mesure et, en `enforce`, celle qui refuse — il y a deux bornes de corps, la nôtre est
  la plus basse ;
- `sessions_max` — le nombre max de sessions MCP ouvertes À LA FOIS par gestionnaire
  (`max_sessions`, 10 000, le défaut de l'SDK → `503` sur une ouverture de plus ; les
  sessions ouvertes ne sont pas touchées).
  **Exception à la règle du 13/10/2026** (« aucun plafond de nombre ») : c'est ce
  plafond qui borne la rétention des sessions dans l'SDK (CVE-2026-59951). Il est
  GLOBAL au processus, sans aucun état par principal ; mesuré le 25/11/2026 sur 7 jours,
  le pic est de 568 sessions ouvertes à la fois — le plafond en vaut 17 fois plus.
  `0` n'est pas accepté (ni pour lui ni pour `corps_mcp`) : il refuserait tout.

**Un seul interrupteur, celui de la limite de débit** (`OTO_DEBIT_MODE`,
`limite_debit.mode_depuis_env`) : `report` (défaut) MESURE et ne refuse rien, `enforce`
applique, `off` coupe la mesure comme la borne. Pas un second interrupteur : la
bascule « mesure → application » est une décision, et elle se prend en un geste.

**Un dépassement** (`depasser`) est compté dans le palmarès quotidien des refus
(`limite_refus_top`, table `limite_refus_jour`), sous son limiteur (`corps`, `outil`,
`sql`, `session`), sa classe et une clé HACHÉE ; et journalisé, une ligne par clé et par
minute au plus, sans jeton, adresse ni identifiant en clair. Lecture au même endroit que
la limite de débit : `oto_admin_monitoring op=transport`, champ `rate_limit_top`.

**Réglages** (`env_inventory`), lus UNE fois (au démarrage, `verifier_configuration`) :
un chiffre illisible ou négatif LÈVE (`ReglageBorneInvalide`) — jamais un défaut deviné.
`0` désarme une borne.
"""
from __future__ import annotations

import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import limite_debit, limite_refus_top

logger = logging.getLogger(__name__)

ENFORCE, REPORT, OFF = limite_debit.ENFORCE, limite_debit.REPORT, limite_debit.OFF

#: Les défauts, tels que décidés le 13/10/2026 (#1232).
CORPS_OCTETS = 4 * 1024 * 1024
CORPS_UPLOAD_OCTETS = 110 * 1024 * 1024
OUTIL_S = 120
SQL_MS = 30_000
SESSION_INACTIVE_S = 30 * 60
#: Le transport de l'SDK `mcp` (25/11/2026) : au-dessus du proxy (32 Mo), et le plafond
#: de sessions par défaut de l'SDK lui-même.
CORPS_MCP_OCTETS = 32 * 1024 * 1024
SESSIONS_MAX = 10_000

#: L'ARRÊT GRACIEUX du serveur (`timeout_graceful_shutdown` d'uvicorn, `server.main`) : sur
#: SIGTERM, uvicorn n'accepte plus rien et laisse finir les requêtes en cours pendant au
#: plus ce délai, puis annule ce qui reste (un flux qui ne finit jamais, par exemple).
#: Sans lui, uvicorn attendait sans fin, et c'est systemd qui tuait au `TimeoutStopSec`
#: sans réponse. Pas une borne d'appel : il ne vaut qu'à l'arrêt du processus.
#: Égal au plafond de vidange du bleu/vert (`BG_DRAIN_MAX` de `deploy/oto-backend.sh`),
#: relevé sur la durée réelle des appels (le plus long
#: appel abouti en 30 jours : 190 s, le 19/11/2026) ; sous le `TimeoutStopSec` des unités
#: de la couleur (5 min), la marge couvrant la fermeture du processus. Garde :
#: `tests/deploy/test_vidange_sans_coupure.py`.
ARRET_GRACIEUX_S = 240

_JOURNAL_INTERVALLE_S = 60.0
_JOURNAL_MAX = 10_000


class ReglageBorneInvalide(RuntimeError):
    """Un réglage d'une borne du processus illisible : levé au démarrage."""


class DelaiOutilDepasse(TimeoutError):
    """Un appel d'outil MCP a dépassé la borne du processus (mode `enforce`). Classé
    `tool_timeout` par `error_taxonomy` : l'agent lit le délai, pas « erreur interne »."""

    def __init__(self, secondes: float) -> None:
        self.secondes = secondes
        super().__init__(f"tool call exceeded {secondes:g} s")


@dataclass(frozen=True)
class Reglages:
    mode: str
    corps_octets: int
    corps_upload_octets: int
    outil_s: int
    sql_ms: int
    session_inactive_s: int
    corps_mcp_octets: int = CORPS_MCP_OCTETS
    sessions_max: int = SESSIONS_MAX

    @property
    def applique(self) -> bool:
        return self.mode == ENFORCE

    @property
    def actif(self) -> bool:
        return self.mode != OFF


def _entier(nom: str, brut: Optional[str], defaut: int) -> int:
    """Absent ou vide : le défaut. Pas un entier, ou négatif : LÈVE. `0` désarme."""
    if brut is None or brut.strip() == "":
        return defaut
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = -1
    if valeur < 0:
        raise ReglageBorneInvalide(
            f"{nom}={brut!r} illisible : un entier positif, `0` désarme la borne.")
    return valeur


def _positif(nom: str, brut: Optional[str], defaut: int) -> int:
    """Comme `_entier`, mais `0` LÈVE aussi : un réglage de l'SDK qui ne se désarme pas
    (`0` refuserait toute requête, ou toute session)."""
    valeur = _entier(nom, brut, defaut)
    if valeur == 0:
        raise ReglageBorneInvalide(
            f"{nom}=0 refuserait tout : un entier strictement positif.")
    return valeur


def lire() -> Reglages:
    """Lit les réglages dans l'environnement. Une lecture littérale par variable :
    l'inventaire (`env_inventory`) les trouve par leur nom."""
    return Reglages(
        mode=limite_debit.mode_depuis_env(),
        corps_octets=_entier("OTO_BORNE_CORPS_OCTETS",
                             os.environ.get("OTO_BORNE_CORPS_OCTETS"), CORPS_OCTETS),
        corps_upload_octets=_entier(
            "OTO_BORNE_CORPS_UPLOAD_OCTETS",
            os.environ.get("OTO_BORNE_CORPS_UPLOAD_OCTETS"), CORPS_UPLOAD_OCTETS),
        outil_s=_entier("OTO_BORNE_OUTIL_S", os.environ.get("OTO_BORNE_OUTIL_S"), OUTIL_S),
        sql_ms=_entier("OTO_MCP_DB_STATEMENT_TIMEOUT_MS",
                       os.environ.get("OTO_MCP_DB_STATEMENT_TIMEOUT_MS"), SQL_MS),
        session_inactive_s=_entier(
            "OTO_BORNE_SESSION_INACTIVE_S",
            os.environ.get("OTO_BORNE_SESSION_INACTIVE_S"), SESSION_INACTIVE_S),
        corps_mcp_octets=_positif(
            "OTO_BORNE_CORPS_MCP_OCTETS",
            os.environ.get("OTO_BORNE_CORPS_MCP_OCTETS"), CORPS_MCP_OCTETS),
        sessions_max=_positif(
            "OTO_BORNE_SESSIONS_MCP_MAX",
            os.environ.get("OTO_BORNE_SESSIONS_MCP_MAX"), SESSIONS_MAX),
    )


@functools.cache
def reglages() -> Reglages:
    """Les réglages du processus, lus une fois. Les bancs repartent par
    `reglages.cache_clear()`."""
    return lire()


def verifier_configuration() -> None:
    """Au démarrage (`server.main`) : un réglage illisible refuse de démarrer ICI."""
    reglages()


_journalise: dict = {}
_journal_verrou = threading.Lock()


def depasser(limiteur: str, classe: str, cle: str, detail: str, mode: str) -> None:
    """Un dépassement de la borne `limiteur` : compté au palmarès quotidien (clé
    HACHÉE par l'appelant, jamais une valeur en clair) et journalisé, une ligne par
    (limiteur, classe, clé) et par minute au plus. Sûr depuis un fil ou la boucle :
    rien n'attend la base (`limite_refus_top.publier`)."""
    murale = time.time()
    limite_refus_top.publier(
        limite_refus_top.PALMARES_BORNES[limiteur].noter(classe, cle, mode, murale))
    marque = (limiteur, classe, cle)
    maintenant = time.monotonic()
    with _journal_verrou:
        if maintenant - _journalise.get(marque, -_JOURNAL_INTERVALLE_S) \
                < _JOURNAL_INTERVALLE_S:
            return
        _journalise.pop(marque, None)
        _journalise[marque] = maintenant
        while len(_journalise) > _JOURNAL_MAX:
            del _journalise[next(iter(_journalise))]
    logger.warning("borne %s %s : %s (classe %s, clé %s…)", limiteur,
                   "appliquée" if mode == ENFORCE else "serait appliquée (report)",
                   detail, classe, cle[:8])
