"""Planity — la SESSION par credential, socle des deux modules d'outils.

Ouvrir une session Planity coûte cher : la chaîne d'auth fait trois allers-retours
HTTP, puis chaque lecture du référentiel ouvre un WebSocket sur le Realtime
Database (le REST de Firebase répond `permission_denied` sur presque tous les
chemins — cf. `oto/tools/planity/README.md` dans oto-core). Rejouer tout ça à
chaque appel d'outil rendrait le connecteur inutilisable.

Un client vivant est donc gardé **par credential**, clé = empreinte SHA-256 de
`email:password` — jamais l'identifiant en clair, la clé de ce dictionnaire vit en
mémoire du processus et se lit dans un dump. Le client est fermé après un temps
d'inactivité. Rien n'est persisté : le credential de référence est au coffre, la
session n'existe qu'en RAM et meurt avec le processus.

⚠️ **Le pool est lié à SA boucle d'événements.** Un `PlanityClient` détient des
WebSockets et un client httpx asynchrone, qui n'ont de sens que dans la boucle où
ils ont été ouverts. Le serveur est mono-loop, donc en production ce cas ne se
présente jamais ; l'entrée d'une autre boucle est traitée comme absente et
abandonnée — jamais rendue, car un client d'une boucle morte échouerait plus tard,
loin d'ici, avec un message qui ne parlerait pas de ça.

⚠️ **Pas de verrou de module.** Un `asyncio.Lock` créé à l'import se lie à la
première boucle qui l'attend et refuse les suivantes. À la place, l'entrée du pool
porte la TÂCHE d'ouverture : deux appels concurrents pour le même credential
trouvent la même tâche et attendent le même client, ce qui est exactement ce
qu'un verrou par clé apportait — sans l'état global qui casse.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError

if TYPE_CHECKING:  # l'annotation seulement — jamais évaluée à l'exécution
    from oto.tools.planity import PlanityClient

log = logging.getLogger("oto_mcp.tools.planity")

#: Un client sans usage depuis ce délai est fermé. Assez long pour qu'une
#: conversation entière réutilise la même session, assez court pour ne pas tenir un
#: WebSocket ouvert chez Planity toute la journée après un appel unique.
_TTL_INACTIF = 30 * 60


@dataclass
class _Entree:
    boucle: asyncio.AbstractEventLoop
    ouverture: "asyncio.Future[PlanityClient]"
    dernier_usage: float


_entrees: dict[str, _Entree] = {}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _eur(cents: int | float | None) -> float:
    """Planity compte en CENTIMES ; on rend des euros à la frontière de l'outil.

    Vit ici parce que les deux modules d'outils en ont besoin : dupliquée, la
    conversion finirait par diverger d'un facteur 100 dans un seul des deux, ce
    qui se lit comme un chiffre d'affaires et pas comme un bug."""
    return round(float(cents or 0) / 100, 2)


def _refus_planity(e: BaseException, geste: str) -> McpError:
    """Traduit un refus de Planity en erreur ACTIONNABLE, ou relaie tel quel.

    Le cas qui compte est le 400 de Firebase à l'étape 1 de la chaîne d'auth : il
    veut dire « cet email ou ce mot de passe ne va pas », et sans cette lecture il
    remonte comme un `HTTPStatusError` brut que l'agent interprète en panne de
    service — donc en « réessaie », alors que réessayer ne peut pas aboutir."""
    if isinstance(e, McpError):
        return e            # déjà actionnable — la retraduire l'appauvrirait
    statut = getattr(getattr(e, "response", None), "status_code", None)
    if statut in (400, 401, 403):
        return _bad(
            f"Planity a refusé {geste} ({statut}) : c'est l'email ou le mot de passe "
            "du compte Planity qui ne convient pas, pas un argument de l'appel — "
            "rejouer à l'identique échouera pareil. Repose le credential `planity` "
            "sur ta page connecteurs, avec les identifiants de `pro.planity.com`.")
    return _bad(f"Planity n'a pas répondu à {geste} : {e}")


async def _ouvrir(email: str, password: str) -> "PlanityClient":
    """Construit un client et VALIDE le credential tout de suite.

    L'authentification est jouée ici, pas au premier appel métier : un credential
    faux doit échouer sur « connexion refusée », pas plus tard sur « ce salon n'est
    pas accessible », qui se lit comme un problème de droits chez Planity."""
    from oto.tools.planity import PlanityClient

    client = PlanityClient(email, password)
    try:
        await client.auth.get_tokens()
    except BaseException:
        await client.close()
        raise
    log.info("planity : session ouverte (pool = %d)", len(_entrees) + 1)
    return client


async def _evincer_les_inactifs(boucle: asyncio.AbstractEventLoop) -> None:
    maintenant = time.monotonic()
    for cle, entree in list(_entrees.items()):
        if entree.boucle is not boucle:
            # Boucle morte : on ne peut ni fermer ni réutiliser. On le DIT.
            log.warning("planity : session abandonnée (boucle d'événements changée)")
            _entrees.pop(cle, None)
            continue
        if maintenant - entree.dernier_usage <= _TTL_INACTIF:
            continue
        _entrees.pop(cle, None)
        if not entree.ouverture.done():
            # Ouverture jamais terminée et pourtant inactive : la laisser courir
            # sans personne pour l'attendre tiendrait un client hors du pool,
            # invisible et jamais fermé.
            entree.ouverture.cancel()
        elif not entree.ouverture.cancelled() and entree.ouverture.exception() is None:
            try:
                await entree.ouverture.result().close()
            # noqa: SILENT — fermeture best-effort d'un client déjà retiré du pool
            except Exception as e:
                log.info("planity : fermeture d'une session inactive en échec (%s)", e)
        log.info("planity : session inactive libérée (pool = %d)", len(_entrees))


async def _client(account: Optional[str] = None) -> PlanityClient:
    """Le client Planity de CET appelant — depuis le pool, ou ouvert à la demande.

    Le nom et l'annotation de retour NON quotée sont un contrat : la sonde de
    version-skew (`tests/test_tools_client_methods_exist.py`) reconnaît les
    fabriques appelées `_client` et lit la classe qu'elles rendent pour vérifier,
    au tag oto-core épinglé, que chaque méthode appelée par les outils existe.
    Renommer cette fonction ou quoter son annotation sort le connecteur de cette
    couverture EN SILENCE — et c'est le connecteur qui en a le plus besoin, son
    cœur vivant dans l'autre dépôt.

    Le credential (email + mot de passe) est résolu au coffre par la cascade
    normale (`byo_user`, palier membre) : la résolution touche la base, donc elle
    part au fil d'exécution, jamais dans la boucle."""
    fields = await asyncio.to_thread(
        access.resolve_credential_fields, "planity", account)
    email, password = fields.get("email"), fields.get("password")
    if not email or not password:
        raise _bad(
            "Le credential `planity` posé est incomplet : il faut l'email ET le mot "
            "de passe du compte `pro.planity.com`. Repose-le sur ta page connecteurs.")

    boucle = asyncio.get_running_loop()
    await _evincer_les_inactifs(boucle)

    cle = hashlib.sha256(f"{email}:{password}".encode()).hexdigest()
    entree = _entrees.get(cle)
    if entree is None or entree.boucle is not boucle:
        entree = _Entree(boucle, asyncio.ensure_future(_ouvrir(email, password)),
                         time.monotonic())
        _entrees[cle] = entree
    try:
        client = await entree.ouverture
    except BaseException as e:
        # Un échec ne se met JAMAIS en cache : sinon un mot de passe corrigé
        # continuerait d'échouer jusqu'à l'expiration du TTL.
        _entrees.pop(cle, None)
        raise _refus_planity(e, "la connexion à ton compte") from e
    entree.dernier_usage = time.monotonic()
    return client
