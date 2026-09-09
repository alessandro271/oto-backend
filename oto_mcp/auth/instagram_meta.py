"""Instagram (statistiques) — obtenir l'autorisation, et la GARDER en vie.

Flux hébergé par oto, sur le patron commun (`connectors/flow` + `auth/flow`) :

1. la personne clique « Connecter » sur la fiche → `_start_flow` rend l'URL du
   dialogue Instagram, avec un `state` signé qui porte son identité ;
2. Instagram la ramène sur `/api/instagram_meta/oauth/callback` (route écrite à la
   main, `api/instagram_meta.py`) ; le code y devient un jeton de 60 jours ;
3. le jeton part au coffre, palier MEMBRE, avec son échéance dans `meta`.

Ce module porte l'ACQUISITION et ce que la fiche affiche. Le service — résoudre
le jeton d'un appel, le renouveler, refuser en nommant la cause — vit dans
`tools/instagram_meta_session.py`, qui importe d'ici. La ligne entre les deux
est celle du déclencheur : un clic humain d'un côté, un appel d'outil ou la
passe quotidienne de l'autre.

⚠️ **CE JETON NE SE RENOUVELLE QUE TANT QU'IL VIT.** Meta n'émet pas de
`refresh_token` sur ce produit : on échange le jeton courant contre un neuf, et
un jeton mort ne s'échange plus. Une connexion oubliée soixante jours n'est pas
dégradée — elle est PERDUE, et seule l'utilisatrice peut la refaire.

C'est ce fait, et lui seul, qui explique les deux mécanismes de renouvellement :

- **à l'usage**, largement en avance (`RENEW_WHEN_REMAINING_DAYS` = 53 jours
  restants sur 60, pas 10) : un usage même très espacé suffit alors à tenir ;
- **et une passe QUOTIDIENNE** (`renouveler_les_jetons`, travail de maintenance
  `instagram-tokens`), parce que le renouvellement paresseux meurt de non-usage.
  Une personne qui ne consulte pas ses statistiques pendant deux mois perdrait sa
  connexion sans avoir rien fait de mal — un mode de panne qu'on ne peut pas
  demander à l'utilisatrice de prévenir.

⚠️ **Les coordonnées de l'application Meta (App ID, secret) ne sont pas dans le
code** : elles vivent en base, au scope PLATEFORME de `connector_settings`, et
c'est l'exploitant de l'instance qui les pose. Ce dépôt est public, et une
application Meta appartient à qui l'a créée — qui répond de ce qu'elle demande et
de qui elle invite comme testeur.

Réglage d'ops : `OTO_MCP_PUBLIC_URL` et `OTO_MCP_OAUTH_STATE_SECRET`, déjà posés
pour les autres flux. L'URL de retour à déclarer chez Meta se LIT
(`connector_flow.callback_url("instagram_meta")`) — jamais écrite en dur : la
preprod et la prod n'ont pas la même, et une URL de prose ment dès qu'on la
relit depuis l'autre.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from .. import credentials_store, status_hints
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.instagram_meta")

CONNECTOR = "instagram_meta"

# Audience du state : un state émis pour ce flux ne vaut QUE pour son callback.
_AUD = "instagram_meta"
_CALLBACK_PATH = "/api/instagram_meta/oauth/callback"

#: Les coordonnées de l'application Meta, sous ce connecteur dans
#: `connector_settings`, scope plateforme.
#:
#: ⚠️ Contrairement aux trois de `planity`, **`app_secret` en est un vrai** : il
#: signe l'échange du code et le passage en jeton long. Il n'est pas au coffre pour
#: autant, et pour la raison inverse de celle qui garde un secret d'utilisateur :
#: le coffre range ce qui appartient à une PERSONNE ou à une ORG, et sa cascade
#: irait le chercher au nom de quelqu'un. Celui-ci n'appartient à personne dans le
#: produit — c'est un réglage de l'instance, comme une clé de service. Il n'est
#: jamais rendu par une API : `oto_admin_connector_setting` est réservé à l'admin
#: de plateforme, et rien de ce module ne l'écrit dans un message ni un journal.
_REGLAGES = ("app_id", "app_secret")

#: La commande qui les pose. Elle vit DANS le message de refus : un diagnostic qui
#: n'indique pas le geste renvoie chercher, et c'est ainsi qu'on relance six fois
#: une configuration valide.
_COMMANDE = ('oto_admin_connector_setting(op="set", connector="instagram_meta", '
             'key="<clé>", value="<valeur>")')


def _coeur():
    """Le paquet `oto.tools.instagram_meta` d'oto-core, importé À L'APPEL.

    Importé ici et pas au chargement du module, pour une raison de PRODUIT : le
    connecteur reste **enregistré** même quand oto-core est trop ancien pour le
    porter. Il est alors visible, sélectionnable, et chaque appel refuse en NOMMANT
    ce qui manque — au lieu de disparaître du catalogue, ce qui ne se remarque pas
    et ne s'explique pas.

    `importlib` et pas `import oto.tools.instagram_meta as …` : `oto` est un
    package d'espace de noms (PEP 420) partagé entre oto-core et oto-cli, et la
    forme `import a.b.c as x` y résout par attribut sur le parent — ce qui échoue
    quand le sous-paquet n'existe pas encore sur ce chemin."""
    import importlib

    try:
        return importlib.import_module("oto.tools.instagram_meta")
    except ImportError as e:
        raise RuntimeError(
            f"Le connecteur `instagram_meta` n'est pas installé sur cette instance : "
            f"le cœur vit dans oto-core et le tag épinglé ne le porte pas encore. "
            f"C'est une configuration de l'instance, pas un problème de ton compte — "
            f"préviens l'exploitant. Détail : {e}") from e


# --- les coordonnées de l'application, posées par l'exploitant -----------------

def _reglages() -> dict:
    """Les coordonnées posées en base, scope plateforme. `{}` si la base est muette.

    Lecture FROIDE et hors boucle : elle n'a lieu qu'au démarrage d'un flux de
    connexion (un clic humain) ou au retour d'un consentement, jamais sur le chemin
    d'un appel d'outil — c'est la propriété que `db/connector_settings` demande de
    tenir. Ce qu'elle achète : une écriture, et toutes les couleurs déployées la
    voient au flux suivant, sans redémarrage ni rechargement par processus."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    """Celles des deux clés qui manquent en base. Vide = tout est là."""
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def app():
    """L'`InstagramApp` de cette instance, ou un refus qui NOMME ce qui manque.

    Le refus est le point : sans lui, le dialogue partirait avec un `client_id`
    vide et Instagram afficherait son écran d'erreur générique — que l'utilisatrice
    lit comme « oto n'a pas le droit », alors que rien ne dépend d'elle."""
    manquantes = coordonnees_manquantes()
    if manquantes:
        raise RuntimeError(
            f"Le connecteur `instagram_meta` n'est pas configuré sur cette "
            f"instance : {', '.join(manquantes)} manquante(s). Ce n'est pas ton "
            f"compte — il n'y a rien à faire de ton côté : préviens l'exploitant de "
            f"l'instance, qui les pose avec {_COMMANDE}.")
    poses = _reglages()
    return _coeur().InstagramApp(app_id=poses["app_id"],
                                 app_secret=poses["app_secret"])


def app_disponible(sub: str) -> bool:
    """`app_ready` du descripteur de flux : y a-t-il de quoi démarrer un dialogue ?

    Ne dépend pas de `sub` — l'application est celle de l'instance, la même pour
    tout le monde — mais la signature du seam le passe, et un connecteur dont
    l'app serait par utilisateur y répondrait autrement."""
    del sub
    return not coordonnees_manquantes()


# --- le state signé ------------------------------------------------------------

def _ctx_org(sub: str) -> int:
    from .. import access  # lazy : évite tout cycle d'import au boot

    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "Aucune org de contexte — impossible de scoper le compte Instagram. "
            "Reconnecte-toi et réessaie.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "") -> str:
    """State signé, LIÉ à l'audience `instagram_meta`.

    Porte `org` parce que le credential est scopé (org, sub) : le callback arrive
    sans en-tête d'auth, ces valeurs doivent voyager avec lui plutôt qu'être
    re-dérivées d'une session vivante. `app` porte le FRONT qui a demandé la
    connexion, déjà réduit par `oauth_flow.resolve_return_app` à une liste
    fermée — le state ne porte jamais une valeur de client non vérifiée."""
    return oauth_flow.sign_state(_AUD, {"sub": sub, "org": org_id, "app": return_app})


def verify_state(state: str) -> Optional[tuple[str, int, str]]:
    """`(sub, org_id, return_app)` si le state est valide, non expiré et émis POUR
    ce flux ; `None` sinon — un callback ne distingue jamais les causes d'un refus.

    `app` absent ou de mauvais type ⇒ `""`, pas un refus du state entier : perdre
    le retour ciblé n'est pas une raison de perdre la connexion."""
    data = oauth_flow.read_state(_AUD, state)
    if not data:
        return None
    sub, org, return_app = data.get("sub"), data.get("org"), data.get("app")
    if not isinstance(sub, str) or not isinstance(org, int):
        return None
    return sub, org, return_app if isinstance(return_app, str) else ""


# --- démarrage du flux ---------------------------------------------------------

def build_auth_url(sub: str, return_app: str = "") -> str:
    """L'URL du dialogue de consentement Instagram pour CETTE personne."""
    org_id = _ctx_org(sub)
    return _coeur().authorize_url(
        app(), oauth_flow.redirect_uri(_CALLBACK_PATH),
        make_state(sub, org_id, oauth_flow.resolve_return_app(return_app)))


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    """Le geste « connecter », déclaré comme celui de tout autre connecteur.

    Une instance non configurée (ou un oto-core trop ancien) est un refus
    d'ENTRÉE, pas une panne : traduit en erreur nommée, l'appelant sait que
    réessayer n'y changera rien. `app` est une clé CACHÉE, passée hors formulaire
    par le front qui sait qui il est — jamais un champ visible."""
    from ..capabilities._types import AuthzDenied

    try:
        return connector_flow.FlowStart(
            auth_url=build_auth_url(ctx.sub, (values or {}).get("app") or ""))
    except RuntimeError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


connector_flow.declare(
    CONNECTOR,
    start=_start_flow,
    label="Autoriser oto chez Instagram",
    callback_path=_CALLBACK_PATH,
    app_ready=app_disponible,
)


# --- le coffre ------------------------------------------------------------------

def _scope(org_id: int, sub: str) -> tuple[str, str]:
    """La ligne de coffre de cette personne dans cette org (palier MEMBRE)."""
    return credentials_store.MEMBER, credentials_store.member_id(org_id, sub)


def persist_grant(sub: str, org_id: int, grant) -> dict:
    """Range le jeton et ses satellites, et rend ce qu'on peut afficher.

    Le SECRET est le jeton long, seul (`secret_kind="oauth"` ⟹ pas de schéma de
    champs, donc le blob est la valeur brute). Tout le reste va dans `meta`, en
    clair et mergeable : `user_id` sans lequel aucun chemin de données ne se
    construit, `username` pour que la fiche dise QUEL compte est connecté, et
    surtout `expires_at` — sans échéance stockée, on ne peut plus que subir
    l'expiration, et c'est précisément ce que ce connecteur ne peut pas se
    permettre."""
    coeur = _coeur()
    maintenant = coeur.utcnow()
    expires_at = coeur.iso(maintenant + timedelta(seconds=grant.expires_in))
    entity_type, entity_id = _scope(org_id, sub)
    credentials_store.set_credential(
        entity_type, entity_id, CONNECTOR, grant.access_token, set_by=sub,
        meta={"user_id": grant.user_id, "username": grant.username,
              "connected_at": coeur.iso(maintenant), "expires_at": expires_at})
    logger.info("instagram_meta : compte connecté (org=%s, expire le %s)",
                org_id, expires_at)
    return {"username": grant.username, "expires_at": expires_at}


def _row(org_id: int, sub: str) -> Optional[dict]:
    entity_type, entity_id = _scope(org_id, sub)
    return credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR)


# --- ce que la fiche affiche ---------------------------------------------------

def _link_state(sub: str) -> connector_link.LinkState:
    """État de lien pour `/api/me`. Mono-compte : une ligne de coffre par membre.

    Le rejet enregistré est LU ICI plutôt que déduit ailleurs — c'est ce qui permet
    à la fiche de dire « autorisation expirée, à reconnecter » sans attendre qu'un
    appel échoue."""
    from .. import access  # lazy

    org = access.current_org(sub)
    if org is None:
        return connector_link.LinkState(linked=False)
    row = _row(org, sub)
    if not row:
        return connector_link.LinkState(linked=False)
    meta = row.get("meta") or {}
    return connector_link.LinkState(
        linked=True, accounts=1, set_at=str(row.get("set_at") or "") or None,
        health_ko=True if meta.get("health_ko") else None,
        health_reason=meta.get("health_reason") or None)


connector_link.register(CONNECTOR, _link_state)


def _etape_manquante(sub: str, org, group, entry: dict) -> Optional[str]:
    """Hook `status_hints` : ce qu'il reste à faire, en un libellé que le front rend
    tel quel — et qui dit à QUI de le faire.

    Trois étapes possibles, et la première n'appartient pas à l'utilisatrice : sans
    coordonnées d'application, le bouton « Connecter » ne peut pas aboutir, et lui
    afficher « Autorise oto » l'enverrait cliquer en boucle sur un dialogue que Meta
    refusera. Un verdict qui désigne la mauvaise personne coûte plus qu'un verdict
    absent."""
    del org, group, entry
    if coordonnees_manquantes():
        return "Application Instagram à configurer par l'exploitant"
    etat = _link_state(sub)
    if not etat.linked:
        return "Autorise oto chez Instagram"
    if etat.health_ko:
        return "Autorisation expirée — reconnecte ton compte"
    return None


status_hints.register(CONNECTOR, _etape_manquante)


def avertir_au_demarrage() -> None:
    """Dit AU BOOT ce qui empêchera le connecteur de servir. Ne lève jamais.

    Le connecteur reste enregistré : sans cette ligne, un exploitant qui n'a pas
    posé les coordonnées ne l'apprendrait qu'au premier clic d'une utilisatrice —
    c'est-à-dire au pire moment, et par elle."""
    try:
        manquantes = coordonnees_manquantes()
    except Exception as e:  # noqa: SILENT — au boot la base peut n'être pas prête
        logger.info("instagram_meta : configuration non vérifiable au démarrage "
                    "(%s) — le premier flux tranchera.", type(e).__name__)
        return
    if manquantes:
        logger.warning(
            "instagram_meta : connecteur monté mais NON configuré — %s "
            "manquante(s). Le bouton « Connecter » refusera en le disant. "
            "Poser : %s", ", ".join(manquantes), _COMMANDE)
