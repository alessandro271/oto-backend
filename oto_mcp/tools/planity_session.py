"""Planity — la SESSION par credential, socle des deux modules d'outils.

Ouvrir une session Planity coûte cher — plusieurs allers-retours réseau avant la
première lecture utile. La rouvrir à chaque appel d'outil rendrait le connecteur
inutilisable.

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

#: Les trois coordonnées de l'application Planity, sous le connecteur `planity`
#: dans `connector_settings`, scope PLATEFORME.
#:
#: ⚠️ **Elles sont publiques par conception, et ce ne sont pas des secrets** : tout
#: navigateur qui ouvre `pro.planity.com` les reçoit. Elles
#: identifient l'application Planity et n'autorisent rien à elles seules — ce qui
#: autorise, c'est le mot de passe de la personne, qui vit au coffre CHIFFRÉ. Elles
#: peuvent donc apparaître dans un message d'erreur ou un journal de débogage sans
#: que ce soit une fuite.
#:
#: C'est aussi pourquoi elles ne sont PAS dans le coffre : y ranger trois
#: identifiants publics, identiques pour tous, garantirait que le prochain lecteur
#: les traite comme un secret — rotation, alerte, temps perdu. Le coffre refuse
#: d'ailleurs une clé plateforme à un connecteur sans `platform` dans ses
#: `auth_modes`, et le lui ajouter ferait annoncer à la fiche qu'une clé plateforme
#: peut servir l'utilisatrice, ce qui est faux.
#:
#: Si elles ne sont pas en dur dans oto-core, c'est par GÉNÉRICITÉ : ce dépôt-là est
#: public et open source, et un client qu'on y publie décrit un protocole — il
#: n'embarque pas les coordonnées d'une entreprise tierce comme s'il était son
#: intégration officielle. Elles appartiennent à Planity ; c'est l'exploitant de
#: l'instance qui les pose et qui répond de ce qu'il appelle.
_REGLAGES = ("firebase_api_key", "firebase_app_id", "rest_api")

#: La commande qui les pose. Elle vit DANS le message de refus : un diagnostic qui
#: n'indique pas le geste renvoie chercher, et c'est ainsi qu'on relance six fois
#: une configuration valide.
_COMMANDE = ('oto_admin_connector_setting(op="set", connector="planity", '
             'key="<clé>", value="<valeur>")')


def _reglages() -> dict:
    """Les coordonnées posées en base, scope plateforme. `{}` si la base est muette.

    Ce que protège la règle de `connector_settings` : **aucune lecture de cette
    table sur le chemin chaud d'un appel d'outil**. Son premier lecteur, la
    cardinalité, est consulté jusqu'à quatre fois par appel, sur un serveur
    mono-loop, contre une base managée distante — une lecture par consultation y
    serait le gel que `docs/event-loop-perf.md` documente, d'où l'instantané en
    mémoire.

    Cette lecture-ci y satisfait, et c'est mesurable : elle n'a lieu qu'à la
    **construction d'un client** Planity — au plus une fois par credential et par
    TTL du pool (30 min), jamais par appel — et elle part au fil d'exécution
    (`asyncio.to_thread` dans `_client`), donc hors de la boucle. Un chemin froid,
    hors boucle : les deux moitiés de la propriété.

    Ce que ça achète : une écriture, et toutes les couleurs déployées (bleu, vert,
    canari) la voient au client suivant — sans redémarrage ET sans `op=reload` par
    processus, qu'un instantané en mémoire aurait exigé. Le réglage d'une instance
    ne doit pas dépendre de qui pense à recharger quoi.
    """
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == "planity"
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    """Celles des trois clés qui manquent en base. Vide = tout est là."""
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def _coeur():
    """Le paquet `oto.tools.planity` d'oto-core, importé À L'APPEL.

    Importé ici et pas au chargement du module, pour une raison de PRODUIT : le
    connecteur reste **enregistré** même quand l'extra `planity` d'oto-core manque
    ou que les coordonnées ne sont pas posées. Il est alors visible, sélectionnable,
    et chaque appel refuse en NOMMANT ce qui manque — au lieu de disparaître du
    catalogue, ce qui ne se remarque pas et ne s'explique pas."""
    # `importlib` et pas `import oto.tools.planity as …` : `oto` est un package
    # d'espace de noms (PEP 420) partagé entre oto-core et oto-cli, et la forme
    # `import a.b.c as x` y résout par attribut sur le parent — ce qui échoue
    # quand le sous-paquet n'existe pas encore sur ce chemin. `import_module`
    # passe par le mécanisme d'import normal et rend ce qui est déjà chargé.
    import importlib

    try:
        coeur = importlib.import_module("oto.tools.planity")
    except ImportError as e:
        raise _bad(
            f"Le connecteur `planity` n'est pas installé sur cette instance : il "
            f"faut l'extra `planity` d'oto-core (`oto-core[planity]`), qui apporte "
            f"`httpx` et `websockets`. C'est une configuration de l'instance, pas "
            f"un problème de ton compte — préviens l'exploitant. Détail : {e}") from e
    return coeur


def endpoints():
    """Les coordonnées de l'application Planity, ou un refus qui NOMME ce qui manque.

    Le refus est le point : un connecteur non configuré doit dire lequel des deux
    côtés est en cause. Sans ça, l'appel échoue plus loin sur un 400 de Firebase,
    que la chaîne d'erreur traduit — correctement, mais faussement ici — en
    « email ou mot de passe refusé ». L'utilisatrice reposerait alors un credential
    parfaitement bon, en boucle. Et il nomme le GESTE, pas seulement le manque."""
    poses = _reglages()
    manquantes = [nom for nom in _REGLAGES if not poses.get(nom)]
    if manquantes:
        raise _bad(
            f"Le connecteur `planity` n'est pas configuré sur cette instance : "
            f"{', '.join(manquantes)} manquante(s). Ce n'est pas ton credential — "
            f"il n'y a rien à reposer de ton côté : préviens l'exploitant de "
            f"l'instance, qui les pose avec {_COMMANDE}.")
    return _coeur().PlanityEndpoints(
        firebase_api_key=poses["firebase_api_key"],
        firebase_app_id=poses["firebase_app_id"],
        rest_api=poses["rest_api"],
    )


def fenetre(date_from, date_to, preset):
    """`(gte_ms, lte_ms)` — la fenêtre de dates, résolue par le cœur."""
    return _coeur().resolve_range(date_from, date_to, preset)


def iso(ms):
    """Un horodatage Planity (millisecondes) en ISO Europe/Paris, ou `None`."""
    return _coeur().ms_to_iso(ms)


def periode(gte_ms, lte_ms) -> dict:
    """La fenêtre couverte, DITE — bornes, nombre de jours, fuseau, et si elle
    s'arrête aujourd'hui.

    Un chiffre d'affaires sans sa fenêtre invite à projeter dessus, et c'est là que
    ça se casse : la plupart des presets s'arrêtent à MAINTENANT, pas à la fin de la
    journée. Le dernier jour est donc partiel, un rythme calculé dessus est trop
    bas, et « au rythme actuel il reste N jours » sort faux sans que rien ne le
    signale. `ends_today` et `complete` sont là pour que l'agent le sache au lieu de
    le supposer, et `days` pour qu'il ne recompte pas une durée qu'on lui donne."""
    import time

    debut, fin = iso(gte_ms), iso(lte_ms)
    aujourdhui = iso(int(time.time() * 1000))[:10]
    jours = None
    if debut and fin:
        from datetime import date

        d, f = date.fromisoformat(debut[:10]), date.fromisoformat(fin[:10])
        jours = (f - d).days + 1
    finit_aujourdhui = bool(fin) and fin[:10] == aujourdhui
    return {
        "from": debut, "to": fin,
        "from_date": debut[:10] if debut else None,
        "to_date": fin[:10] if fin else None,
        "days": jours,
        "timezone": "Europe/Paris",
        "ends_today": finit_aujourdhui,
        # Le dernier jour n'est pas fini : tout rythme journalier calculé sur cette
        # fenêtre le sous-estime, et une projection bâtie dessus avec.
        "complete": not finit_aujourdhui,
    }


def avertir_au_demarrage() -> None:
    """Dit AU BOOT ce qui empêchera le connecteur de servir. Ne lève jamais.

    Le connecteur reste enregistré : sans cette ligne, un opérateur qui a oublié
    une variable ne l'apprendrait qu'au premier appel d'une utilisatrice."""
    try:
        manquantes = coordonnees_manquantes()
    except Exception as e:  # noqa: SILENT — au boot la base peut n'être pas prête ; l'appel, lui, dira tout
        log.info("planity : configuration non vérifiable au démarrage (%s) — le "
                 "premier appel tranchera.", type(e).__name__)
        manquantes = []
    if manquantes:
        log.warning(
            "planity : connecteur monté mais NON configuré — %s manquante(s). Les "
            "outils `planity_*` refuseront en le disant. Poser : %s",
            ", ".join(manquantes), _COMMANDE)
    try:
        _coeur()
    except McpError as e:
        log.warning(
            "planity : connecteur monté mais le cœur n'est pas installable — "
            "installer l'extra `oto-core[planity]` (httpx + websockets). Les "
            "outils `planity_*` refuseront en le disant. Détail : %s", e)

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

    Vit ici parce que les quatre modules d'outils en ont besoin : dupliquée, la
    conversion finirait par diverger d'un facteur 100 dans un seul d'entre eux, ce
    qui se lit comme un chiffre d'affaires et pas comme un bug."""
    return round(float(cents or 0) / 100, 2)


def _eur_ou_rien(cents: int | float | None) -> float | None:
    """Comme `_eur`, mais un montant ABSENT reste absent au lieu de valoir zéro.

    `_eur(None)` rend `0.0`, ce qui est juste quand le zéro est un vrai zéro et faux
    partout ailleurs : une prestation sans prix devient offerte, un lot sans prix
    d'achat devient gratuit, et on calcule une marge dessus. Un `None` qui traverse
    dit « on ne sait pas » ; un `0.0` prétend savoir, et ne lève jamais."""
    if cents is None:
        return None
    return round(float(cents) / 100, 2)


def _refus_planity(e: BaseException, geste: str) -> McpError:
    """Traduit un refus de Planity en erreur ACTIONNABLE, ou relaie tel quel.

    Le cas qui compte est le 400 de Firebase à l'étape 1 de la chaîne d'auth : il
    veut dire « cet email ou ce mot de passe ne va pas », et sans cette lecture il
    remonte comme un `HTTPStatusError` brut que l'agent interprète en panne de
    service — donc en « réessaie », alors que réessayer ne peut pas aboutir.

    ⚠️ **Le texte d'une exception amont ne traverse JAMAIS cette frontière.** Ce
    connecteur est le seul dont le credential est un MOT DE PASSE, et il le passe
    en argument à trois fonctions : n'importe quelle exception construite avec ces
    arguments — un `RuntimeError(f"... {email} / {password}")` d'une version
    ultérieure du cœur, d'une lib intermédiaire, d'un cas d'erreur qu'on n'a pas
    écrit — se serait retrouvée mot pour mot dans la réponse rendue à l'agent, donc
    dans un transcript et dans le journal d'appels. Aucun cas réel ne le fait
    aujourd'hui (httpx met l'URL dans ses messages, jamais le corps de la requête ;
    mesuré) : c'est bien pour ça qu'il fallait fermer la porte AVANT d'avoir un
    incident à raconter. On rend donc le TYPE et, s'il existe, le statut HTTP —
    jamais le message. Ce qu'on perd au diagnostic, le journal serveur l'a."""
    if isinstance(e, McpError):
        return e            # déjà actionnable — la retraduire l'appauvrirait
    statut = getattr(getattr(e, "response", None), "status_code", None)
    if statut in (400, 401, 403):
        return _bad(
            f"Planity a refusé {geste} ({statut}) : c'est l'email ou le mot de passe "
            "du compte Planity qui ne convient pas, pas un argument de l'appel — "
            "rejouer à l'identique échouera pareil. Repose le credential `planity` "
            "sur ta page connecteurs, avec les identifiants de `pro.planity.com`.")
    porte = f" (HTTP {statut})" if statut else ""
    log.warning("planity : %s a échoué — %s%s", geste, type(e).__name__, porte)
    return _bad(
        f"Planity n'a pas répondu à {geste} : {type(e).__name__}{porte}. Ce n'est "
        "pas un refus d'identifiants — réessaie, et si ça dure, c'est chez Planity "
        "que ça se passe.")


async def _ouvrir(email: str, password: str, coordonnees) -> "PlanityClient":
    """Construit un client et VALIDE le credential tout de suite.

    L'authentification est jouée ici, pas au premier appel métier : un credential
    faux doit échouer sur « connexion refusée », pas plus tard sur « ce salon n'est
    pas accessible », qui se lit comme un problème de droits chez Planity."""
    client = _coeur().PlanityClient(email, password, coordonnees)
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
            # Même règle qu'au-dessus : le TYPE, jamais le texte — cette
            # exception naît sur un client construit avec le mot de passe.
            except Exception as e:
                log.info("planity : fermeture d'une session inactive en échec (%s)",
                         type(e).__name__)
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
    # Les coordonnées de l'instance AVANT le credential : une instance non
    # configurée doit le dire, pas accuser le mot de passe de l'utilisatrice.
    # Les deux lectures partent au FIL D'EXÉCUTION : elles touchent la base, et ce
    # serveur est mono-loop (`docs/event-loop-perf.md`).
    coordonnees = await asyncio.to_thread(endpoints)
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
        entree = _Entree(
            boucle, asyncio.ensure_future(_ouvrir(email, password, coordonnees)),
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
