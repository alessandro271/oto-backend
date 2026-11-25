"""La garde des rôles PLATEFORME : un opérateur n'exerce son rôle qu'en session
interactive, avec un second facteur enrôlé (CASA AL1, exigence 3.3.1 : « le MFA est
imposé à tout compte d'administration »).

Ce qu'elle borne : `admin` et `super_admin` — donc toutes les surfaces
d'administration de la plateforme, qui dérivent toutes du même point :
`access.scope.role_effectif`. Les règles `PLATFORM_ADMIN`, `SUPER_ADMIN`,
`LIBRARY_PUBLISHER`, `PLATFORM_OPERATOR_OPT`, la branche opérateur
d'`ORG_ADMIN_OF_OR_OPERATOR` et des règles de tenant, l'escalade
`roles.is_platform_admin` (un super_admin passe toute règle `ORG_ADMIN_OF*`), la
consultation « en tant que » et son écriture acceptée (`api/routes.py`), la visibilité
des `oto_admin_*` : rien de tout cela ne relit le rôle ailleurs. Borner ce point-là,
c'est les borner toutes — et une règle neuve l'est sans rien déclarer.

La garde ramène le rôle à `member`, exactement comme le verrou des jetons de
délégation (`verrou_org.role_plateforme`), quand le PORTEUR de la requête :

- présente un jeton d'API `oto_` (utilisateur ou clé d'org) : un jeton ne passe
  aucun second facteur, il vit 90 jours, il fuit avec un fichier de configuration ;
- présente un jeton de DÉLÉGATION (travail du runner), sauf pendant l'appel d'un
  `oto_admin_*` que son agent NOMME (`verrou_org.outil_admin_permis`, chemin tracé) —
  le seul chemin non interactif qui garde le rôle, et il exige lui aussi le facteur
  enrôlé de son porteur ;
- présente une session Logto (JWT) d'un compte qui n'a AUCUN second facteur fort
  enrôlé (`Totp` ou `WebAuthn`, lus par l'API de gestion de Logto), ou dont on ne peut
  pas le savoir (Logto injoignable, compte d'un tenant tiers) : **fail-closed**.

Pourquoi « enrôlé » suffit à dire « utilisé » : Logto re-vérifie le facteur enrôlé à
CHAQUE connexion (`guardMfaVerificationStatus`, `docs/auth-logto.md` §MFA par org), et
il ne porte pas le niveau d'authentification dans le jeton d'accès (pas d'`amr`, pas de
step-up `acr_values`, SECURITY.md §10) — le lire demanderait de modifier le script de
claims du tenant. Limites dites dans `docs/auth-logto.md` §Administration de la
plateforme.

**Le porteur seul est concerné** (`sub` = celui qui a présenté le bearer) : lire le
rôle d'un TIERS (liste des opérateurs, membres d'une org) garde son chemin. **Hors
requête** (script, maintenance, tâche de fond), aucun porteur : rien n'est borné — la
garde vise ce qui arrive par le réseau. Le porteur se pose au bord : la face REST dans
`api/base._authenticate` (chaque branche), la face MCP se lit sur le jeton vérifié par
FastMCP (`get_access_token`).

**Réglage d'instance, éteint par défaut** — `OTO_ADMIN_MFA` : non posé (ou vide) = `off`,
le tronc inchangé (un opérateur exerce son rôle comme avant, sous jeton compris) ; une
instance s'y inscrit avec `report` (journalise ce qui serait retiré, ne retire rien : le
relevé à faire avant de fermer) puis `enforce`. Une valeur POSÉE mais inconnue vaut
`enforce`, sur le modèle des verrous de délégation : une faute de frappe dans une
déclaration qui voulait fermer ne doit pas ouvrir l'administration.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_ENV = "OTO_ADMIN_MFA"
_MODES = ("enforce", "report", "off")

#: Les rôles bornés — ceux d'un compte d'ADMINISTRATION de la plateforme.
ROLES_BORNES = ("admin", "super_admin")

#: Les facteurs qui comptent — une POLITIQUE, pas un constat sur Logto : seuls TOTP et
#: WebAuthn ouvrent l'administration. `BackupCode` seul ne compte pas (Logto ne l'enrôle
#: qu'avec un autre), et un code par SMS ou e-mail ne compte pas non plus : un
#: opérateur s'équipe d'une application TOTP ou d'une clé d'accès.
FACTEURS_FORTS = frozenset({"Totp", "WebAuthn"})

# Natures de porteur.
SESSION = "session"          # JWT Logto vérifié : une personne s'est connectée
JETON = "jeton"              # jeton d'API `oto_` (utilisateur, clé d'org)
DELEGATION = "delegation"    # jeton d'un travail du runner
MACHINE = "machine"          # secret de worker, identité de service : aucun rôle

# Codes de refus — un par cause, chacun DIT le geste qui débloque.
CODE_SESSION = "admin_interactive_session_required"
CODE_MFA = "admin_mfa_required"
CODE_INVERIFIABLE = "admin_mfa_unverifiable"
CODES = frozenset({CODE_SESSION, CODE_MFA, CODE_INVERIFIABLE})

_PHRASES = {
    CODE_SESSION: (
        "Platform administration requires an interactive sign-in: API tokens and "
        "hosted-agent tokens are refused on admin surfaces. Sign in to the dashboard "
        "(or connect your MCP client through OAuth) with your second factor."),
    CODE_MFA: (
        "Platform administration requires multi-factor authentication, and this "
        "account has no second factor enrolled. Enrol an authenticator app (TOTP) or "
        "a passkey in your account security settings, then sign out and sign in "
        "again."),
    CODE_INVERIFIABLE: (
        "Platform administration requires multi-factor authentication, and the "
        "second factor of this account could not be verified with the identity "
        "provider right now. Access is refused until it can be — retry in a minute."),
}

# Durées du cache des verdicts, en secondes. Un verdict POSITIF vit peu : un facteur
# retiré cesse d'ouvrir en cinq minutes au plus. Une PANNE vit moins encore : un Logto
# en panne ne doit ni ouvrir (fail-closed) ni être martelé à chaque prédicat.
TTL_OK = 300
TTL_SANS_FACTEUR = 60
TTL_PANNE = 10
#: Délai de l'appel à Logto : court, il est payé dans le fil d'une requête.
DELAI_LOGTO_S = 5


@dataclass(frozen=True)
class Porteur:
    """Qui a présenté le bearer de la requête en cours — jamais sa valeur."""
    nature: str
    # Le sub CANONIQUE du porteur (celui sous lequel ses rôles se lisent).
    sub: Optional[str]
    # L'identifiant Logto du JWT vérifié (session seule) : c'est LUI dont on lit les
    # facteurs, pas le canonique — un ancien identifiant redirigé n'a plus de ligne.
    sub_logto: Optional[str] = None


@dataclass(frozen=True)
class Motif:
    code: str
    message: str


# Posé par la face REST à chaque authentification (y compris à `MACHINE` pour un
# worker ou un service) ; jamais posé par la face MCP, qui se lit sur son jeton.
_PORTEUR_REST: contextvars.ContextVar[Optional[Porteur]] = contextvars.ContextVar(
    "oto_garde_admin_porteur", default=None)
_SIGNALE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_garde_admin_signale", default=None)

_cache: dict[str, tuple[float, Optional[str]]] = {}
_verrou_cache = threading.Lock()
# Un verrou PAR COMPTE autour de la lecture Logto : cent requêtes d'un opérateur qui
# tombent sur un cache échu font UN appel, pas cent fils bloqués sur un Logto lent.
_verrous_compte: dict[str, threading.Lock] = {}


def mode() -> str:
    """Le mode de la garde, lu à chaque appel. Non posé ou vide : `off` (le tronc
    inchangé, l'instance s'inscrit). Posé mais inconnu : `enforce` — une faute de frappe
    ne doit pas ouvrir l'administration."""
    m = (os.environ.get(_ENV) or "").strip().lower()
    if not m:
        return "off"
    return m if m in _MODES else "enforce"


def poser(porteur: Optional[Porteur]) -> contextvars.Token:
    """Pose le porteur de la requête REST — à chaque authentification, pour qu'aucun
    porteur ne survive à sa requête."""
    return _PORTEUR_REST.set(porteur)


def _porteur_mcp() -> Optional[Porteur]:
    """Le porteur d'une requête MCP, lu sur le jeton que FastMCP a vérifié — None
    hors requête MCP authentifiée."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
    # noqa: SILENT — hors requête MCP : aucun jeton, donc aucun porteur (rien à borner)
    except Exception:
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    sub = claims.get("sub")
    # Un jeton d'API se reconnaît POSITIVEMENT (`server._verify_api_token` le marque) :
    # tout le reste est un JWT vérifié par la chaîne Logto.
    if (getattr(token, "client_id", None) == "oto_api_token"
            or claims.get("token_id") is not None or claims.get("token_kind")):
        nature = DELEGATION if claims.get("token_kind") == "delegation" else JETON
        return Porteur(nature, sub)
    canonique = sub
    from .tenant_migration import alias_drain_armed
    if sub and alias_drain_armed():
        from .auth.hooks import current_user_sub_from_token
        canonique = current_user_sub_from_token()
    return Porteur(SESSION, canonique, sub_logto=sub)


def courant() -> Optional[Porteur]:
    """Le porteur de la requête en cours, ou None (hors requête)."""
    p = _PORTEUR_REST.get()
    return p if p is not None else _porteur_mcp()


def _facteurs_logto(sub_logto: str) -> list[str]:
    """Les types de facteurs enrôlés (seam de test). Lève sur toute panne — un sub
    d'un tenant tiers compris (`require_primary_tenant`)."""
    from .auth.annuaire import facteurs_2fa
    return [str(f.get("type")) for f in facteurs_2fa(sub_logto, delai=DELAI_LOGTO_S)]


def oublier(sub_logto: Optional[str] = None) -> None:
    """Vide le verdict d'un compte (facteurs réinitialisés), ou tout le cache."""
    with _verrou_cache:
        if sub_logto is None:
            _cache.clear()
        else:
            _cache.pop(sub_logto, None)


def _verdict_mfa(sub_logto: Optional[str]) -> Optional[str]:
    """None si le compte a un facteur fort enrôlé ; sinon le CODE du refus."""
    if not sub_logto:
        return CODE_INVERIFIABLE
    with _verrou_cache:
        hit = _cache.get(sub_logto)
        verrou = _verrous_compte.setdefault(sub_logto, threading.Lock())
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    with verrou:
        with _verrou_cache:
            hit = _cache.get(sub_logto)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        return _lire_et_cacher(sub_logto)


def _lire_et_cacher(sub_logto: str) -> Optional[str]:
    now = time.monotonic()
    try:
        types = _facteurs_logto(sub_logto)
    except Exception as e:  # noqa: BLE001 — fail-closed, nommé et journalisé
        logger.warning("garde admin : facteurs MFA illisibles pour %s (%s: %s) — "
                       "rôle plateforme refusé", sub_logto, type(e).__name__,
                       str(e)[:200])
        code, ttl = CODE_INVERIFIABLE, TTL_PANNE
    else:
        if FACTEURS_FORTS.intersection(types):
            code, ttl = None, TTL_OK
        else:
            code, ttl = CODE_MFA, TTL_SANS_FACTEUR
    with _verrou_cache:
        _cache[sub_logto] = (now + ttl, code)
    return code


def _motif_du_porteur(p: Porteur) -> Optional[str]:
    if p.nature == SESSION:
        return _verdict_mfa(p.sub_logto)
    if p.nature == DELEGATION:
        from . import verrou_org
        if not verrou_org.outil_admin_permis(p.sub):
            return CODE_SESSION
        # Le travail agit au nom de son porteur : celui-ci doit être un compte protégé.
        return _verdict_mfa(p.sub)
    # Jeton d'API, et toute nature inconnue : refusé.
    return CODE_SESSION


def motif(sub: Optional[str], role: str) -> Optional[Motif]:
    """Pourquoi `role` serait retiré à `sub` sous la requête en cours — None s'il ne
    l'est pas (rôle non borné, tiers, hors requête, garde coupée)."""
    if role not in ROLES_BORNES or not sub:
        return None
    if mode() == "off":
        return None
    p = courant()
    if p is None or p.sub is None or str(sub) != str(p.sub):
        return None
    if p.nature == MACHINE:
        return None     # un worker ou un service n'a aucun rôle de compte à borner
    code = _motif_du_porteur(p)
    return Motif(code, _PHRASES[code]) if code else None


def borner(sub: Optional[str], role: str) -> str:
    """Le rôle plateforme de `sub` après la garde — `member` à la place d'un rôle
    d'administration que la requête ne peut pas exercer (appelé par
    `access.scope.role_effectif`, en DERNIER)."""
    m = motif(sub, role)
    if m is None:
        return role
    if mode() == "report":
        if _SIGNALE.get() != m.code:
            _SIGNALE.set(m.code)
            logger.warning("garde admin (report) : %s agit avec le rôle `%s` — "
                           "`enforce` le lirait `member` (%s)", sub, role, m.code)
        return role
    return "member"


def motif_brut(sub: Optional[str], exige: tuple[str, ...] = ROLES_BORNES
               ) -> Optional[Motif]:
    """Le motif de la garde pour le rôle NON borné de `sub`, s'il est l'un de `exige` —
    ce que lit une règle d'autorisation pour DIRE pourquoi elle refuse un opérateur.
    Un compte qui n'aurait pas le rôle même garde levée n'a pas de motif : son refus
    reste celui de la règle."""
    if not sub or mode() == "off":
        return None
    # Sans porteur concerné, aucun motif — et aucune lecture du rôle en base : le
    # refus d'une règle n'a pas à payer une requête pour ne rien dire de plus.
    p = courant()
    if p is None or p.sub is None or str(sub) != str(p.sub) or p.nature == MACHINE:
        return None
    from . import access
    role = access.role_hors_garde(sub)
    return motif(sub, role) if role in exige else None


# ── La nomination : nul n'est NOMMÉ admin sans second facteur fort ──────────────
#
# La garde ci-dessus juge un opérateur à chaque requête ; celle-ci juge le GESTE qui
# le fait opérateur. Sans elle, un compte sans facteur nommé `admin` sous `enforce`
# porterait un rôle qu'il ne peut exercer, et sous `report` le relevé le découvrirait
# après coup. Même mode, même sémantique : `off` ne lit rien (le tronc inchangé),
# `report` journalise et laisse passer, `enforce` refuse — facteurs illisibles compris
# (fail-closed). Lecture FRAÎCHE chez Logto (le cache est réécrit, pas consulté) : le
# geste est rare, et le verdict ne doit pas dater de cinq minutes.

CODE_PROMOTION = "admin_mfa_required_for_promotion"

_PHRASES_PROMOTION = {
    CODE_MFA: (
        "This account has no strong second factor enrolled (authenticator app or "
        "passkey; backup codes alone do not count). It cannot be made a platform "
        "administrator until it enrols one."),
    CODE_INVERIFIABLE: (
        "The second factor of this account could not be verified with the identity "
        "provider right now. It cannot be made a platform administrator until it can "
        "be — retry in a minute."),
}


def refus_promotion(sub: Optional[str], role: str) -> Optional[Motif]:
    """Pourquoi `sub` ne peut pas être NOMMÉ `role` — None si la nomination passe
    (rôle non borné, garde coupée, `report`, facteur fort enrôlé). Appelé par tout
    chemin qui écrit `users.role` vers un rôle d'administration."""
    if role not in ROLES_BORNES:
        return None
    m = mode()
    if m == "off":
        return None
    code = _lire_et_cacher(str(sub)) if sub else CODE_INVERIFIABLE
    if code is None:
        return None
    if m == "report":
        logger.warning("garde admin (report) : %s nommé `%s` sans second facteur fort — "
                       "`enforce` refuserait la nomination (%s)", sub, role, code)
        return None
    return Motif(CODE_PROMOTION, _PHRASES_PROMOTION[code])
