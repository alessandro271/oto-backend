"""La connexion menée par l'agent : l'adresse du SEUL geste qui reste à une personne.

Contrat commun aux deux plateformes qui servent les connecteurs de la lib : `oto-now/connectors`,
`docs/conception/refus-de-connexion.md`. Quand un appel échoue parce qu'un connecteur est coupé,
n'a pas de compte ou demande une reconnexion, le refus porte `reason`, `gesture`, `connector`,
`space` et `url` ; la capacité `connector.connect` (`oto_connector_connect`) rend la même
adresse sur demande. La personne n'a plus qu'à ouvrir l'adresse et faire son geste : consentir,
coller une clé, brancher son compte, ou, administrateur, rouvrir le connecteur.

**L'adresse est une page du tableau de bord, jamais un départ de consentement.** Elle ne porte
ni code, ni `state`, ni jeton : on peut la relayer, l'afficher, la journaliser. Son ouverture
(GET) ne lance rien et n'écrit rien : la page montre la fiche du connecteur, et c'est son
bouton qui lance le geste, par un POST authentifié par la session de la personne
(`POST /api/me/connectors/{name}/connect`, dont le `state` est lié à qui l'a lancé, oto-backend
#1914 ; ou la pose d'une clé). L'espace qu'elle nomme (`space`) n'est qu'une indication : la
page relit l'org de son chemin, et chaque geste revérifie les droits côté serveur.

**Les pages, par espace** (types de liens de `links`, déclarés par l'instance ou le tenant) :
`connect_member` (la fiche d'un connecteur, pour sa propre clé ou son propre consentement),
`connect_org` et `connect_team` (les connecteurs de l'org, ou d'une équipe, ouverts sur ce
connecteur, pour un administrateur). Un produit qui n'en déclare pas n'a PAS d'adresse à
donner : c'est une erreur dite (`PageDeConnexionAbsente`), jamais un repli vers une page qui
n'existe pas chez lui, ni vers la nôtre.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from .. import links, providers
from . import flow as connector_flow

logger = logging.getLogger(__name__)

REASONS = ("connector_off", "no_account", "reconnect")
GESTURES = ("oauth", "key", "hosted_link", "admin_activation")

# Le porteur des comptes de messagerie hébergés (un lien du fournisseur rattache le compte).
_PORTEUR_HEBERGE = "unipile"

# Le type de lien de la page du geste, par sorte d'espace.
_PAGE = {"me": "connect_member", "org": "connect_org", "team": "connect_team"}


class PageDeConnexionAbsente(LookupError):
    """Le produit de ce compte ne déclare pas la page où se fait le geste : aucune adresse à
    donner. `raison` dit pourquoi (`links.raison_sans_lien`)."""

    code = "connect_page_undeclared"

    def __init__(self, connector: str, raison: str, *, declaree: bool = False):
        super().__init__(
            f"No address can be given to connect `{connector}`: {raison}. Whoever runs "
            f"this instance declares the connection pages in OTO_DASHBOARD_LINK_PATHS "
            f"(`connect_member`, `connect_org`, `connect_team`).")
        self.connector = connector
        self.raison = raison
        # The page IS declared but cannot be rendered here (no org in this context):
        # a context without an org, not an instance to fix.
        self.declaree = declaree


def geste(connector: str) -> Optional[str]:
    """Le geste qui donne un compte à `connector` : `oauth` (un flux de consentement déclaré,
    `connectors.flow`), `hosted_link` (un compte de messagerie rattaché par le lien du
    fournisseur), `key` (une clé ou des identifiants à saisir), ou `None` quand le connecteur
    n'a pas de compte du tout (`secret_kind="none"`)."""
    porteur = providers.credential_provider(connector)
    if porteur == _PORTEUR_HEBERGE and connector != _PORTEUR_HEBERGE:
        return "hosted_link"
    if connector_flow.supports(connector) or connector_flow.supports(porteur):
        return "oauth"
    if porteur in providers.CREDENTIAL_PROVIDERS:
        return "key"
    return None


def espace(space: str) -> tuple[str, Optional[str]]:
    """`me` → `("me", None)`, `org` → `("org", None)`, `team:<id>` → `("team", "<id>")`.
    Lève `ValueError` sur toute autre forme."""
    if space in ("me", "org"):
        return space, None
    sorte, _, reste = space.partition(":")
    if sorte == "team" and reste and ":" not in reste and not any(c.isspace() for c in reste):
        return "team", reste
    raise ValueError(f"`space` must be `me`, `org` or `team:<id>`, got {space!r}")


def espace_d_equipe(group_id) -> str:
    return f"team:{group_id}"


def _avec_query(url: str, **params) -> str:
    """`url` avec `params` ajoutés à sa query (ceux qui y sont déjà gardés)."""
    p = urlsplit(url)
    ajout = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    query = "&".join(q for q in (p.query, ajout) if q)
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))


def adresse(connector: str, *, sub: Optional[str], org, space: str = "me") -> str:
    """L'adresse de la page où se fait le geste pour `connector` dans `space`. Lève
    `PageDeConnexionAbsente` si le produit du compte n'a pas cette page, ou si elle demande
    un paramètre que le contexte n'a pas (une org)."""
    sorte, equipe = espace(space)
    kind = _PAGE[sorte]
    params = {"org": org, "connector": connector}
    if equipe is not None:
        params["group"] = equipe
    url = links.link_for(kind, sub=sub, **params)
    if not url:
        raise PageDeConnexionAbsente(
            connector, links.raison_sans_lien(kind, sub=sub, **params) or "no page",
            declaree=links.lien_declare(kind, sub=sub))
    return _avec_query(url, space=space)


def details(reason: str, connector: str, *, sub: Optional[str], org,
            space: str = "me", gesture: Optional[str] = None) -> dict:
    """Les champs du contrat pour un refus : `reason`, `gesture`, `connector`, `space`,
    `url`. `gesture` par défaut : `admin_activation` pour un connecteur coupé, sinon le geste
    du connecteur. Lève `PageDeConnexionAbsente` (pas d'adresse) ou `ValueError` (un
    connecteur sans compte n'a ni `no_account` ni `reconnect`)."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    if gesture is None:
        gesture = "admin_activation" if reason == "connector_off" else geste(connector)
    if gesture not in GESTURES:
        raise ValueError(f"`{connector}` has no account to connect")
    return {"reason": reason, "gesture": gesture, "connector": connector, "space": space,
            "url": adresse_du_geste(connector, gesture, sub=sub, org=org, space=space)}


def adresse_du_geste(connector: str, gesture: str, *, sub: Optional[str], org,
                     space: str = "me") -> str:
    """`adresse`, sur la bonne fiche : une clé se pose sur la fiche de son PORTEUR (le
    compte d'un fournisseur qu'empruntent ses services ou ses canaux) ; un consentement,
    un compte hébergé ou une activation, sur la fiche du connecteur lui-même."""
    fiche = providers.credential_provider(connector) if gesture == "key" else connector
    return adresse(fiche, sub=sub, org=org, space=space)


def details_du_refus(reason: str, connector: str, *, sub: Optional[str], org,
                     space: str = "me", gesture: Optional[str] = None) -> dict:
    """`details`, pour un refus DÉJÀ en cours : le refus d'origine ne doit pas être remplacé
    par une autre erreur. Sans page déclarée, rien n'est ajouté au refus, et l'écart est
    journalisé en ERREUR (une instance mal déclarée, à corriger), jamais tu."""
    try:
        return details(reason, connector, sub=sub, org=org, space=space, gesture=gesture)
    except PageDeConnexionAbsente as e:
        (logger.info if e.declaree else logger.error)(
            "connect link not built for %s (%s): %s", connector, reason, e)
        return {}
    except ValueError as e:
        logger.error("connect link not built for %s (%s): %s", connector, reason, e)
        return {}


def sans_compte(cls, connector: str, *, sub: Optional[str], org, message: str):
    """Le refus « aucun compte » (`cls`, une `McpError`) de `connector`, portant le contrat
    (`reason: no_account`) : son message finit par l'adresse, ses champs sont posés pour
    l'enveloppe. Sans adresse possible, le refus d'origine, inchangé."""
    from mcp.types import ErrorData, INVALID_PARAMS
    d = details_du_refus("no_account", connector, sub=sub, org=org)
    return poser(cls(ErrorData(code=INVALID_PARAMS, message=message + phrase(d))), d)


def phrase(d: dict) -> str:
    """La fin du message d'un refus qui porte une adresse : le contrat veut que le message se
    termine par `open <url>`, pour qu'un agent qui ne lit pas les données relaie le geste."""
    return f" To connect, open {d['url']}" if d.get("url") else ""


def poser(exc: BaseException, d: dict) -> BaseException:
    """Pose les champs du contrat sur un refus (`exc.oto_connect`) : l'enveloppe d'erreur MCP
    (`middleware/error_envelope`) les recopie dans `data.oto`."""
    if d:
        exc.oto_connect = dict(d)  # type: ignore[attr-defined]
    return exc


def lu(exc: BaseException) -> dict:
    """Les champs du contrat posés sur la chaîne d'exceptions d'un refus, ou `{}`."""
    vus: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in vus:
        vus.add(id(e))
        d = getattr(e, "oto_connect", None)
        if isinstance(d, dict) and d:
            return dict(d)
        e = e.__cause__ or e.__context__
    return {}
