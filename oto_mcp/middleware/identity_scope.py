"""`IdentityScopeMiddleware` — l'identité résolue UNE fois par message, hors boucle.

## Ce qu'il corrige

`auth.hooks.current_user_sub_from_token()` canonicalise le `sub` du jeton (drain
d'alias, ADR 0052) par **deux requêtes PostgreSQL SYNCHRONES** : un `SELECT` sur
`sub_aliases`, puis un `INSERT … ON CONFLICT` sur `users` — donc un **COMMIT**. Elles
partent depuis le thread de la boucle, et la fonction n'a aucune mémoire : **chaque
appelant les repaie**.

Or la même identité est redemandée par tout le monde sur le chemin d'UN seul appel —
le nom d'outil du tenant (`middleware/alias`), le refus de compte en pause
(`middleware/account_suspended`), le contexte d'appel (`middleware/call_context`), le
filtrage per-user (`middleware/disabled_tools`), les instructions d'org
(`middleware/dynamic_instructions`), et deux fois le journal (`server._calllog_sink`
et `_calllog_identity`). **Mesuré le 2026-09-09 sur la chaîne réellement servie : 10
allers-retours PG par `tools/call`, dont 5 COMMIT, tous dans la boucle** — 10 des 14
allers-retours de l'appel, pour UNE valeur. `py-spy` sur la production le même jour :
38 % du temps de boucle occupé, dont les deux tiers en SQL synchrone.

## Le précédent, dans le même fichier

`server._calllog_sink` sort déjà son insertion par `await asyncio.to_thread(...)`,
avec le commentaire « ne pas geler l'event loop sur le chemin chaud de chaque tool
call ». La ligne **juste au-dessus** faisait ses deux requêtes dedans. Le remède avait
été posé sur l'insertion et avait manqué l'identité à côté ; c'est le même remède,
posé au bon endroit.

## Pourquoi ici, et pas plus bas

Ce middleware est **le plus EXTERNE des NÔTRES** — au-dessus même de
`ToolAliasMiddleware`, qui se déclare « outermost absolu » pour le NOM de l'outil.
⚠️ Pas « le plus externe de tous » : `fastmcp` pose le sien (`DereferenceRefsMiddleware`)
dans son constructeur, donc avant tout enregistrement. Il ne demande aucune identité, ce
qui rend l'écart inoffensif — mais une phrase fausse dans un docstring finit par être
crue.

`ToolAlias` et celui-ci ne se disputent rien : celui-ci ne lit ni ne réécrit aucun nom,
aucun argument, aucun résultat. Il ouvre une portée et la referme. Mais il doit être
au-dessus d'`alias`, parce qu'`alias` est le premier à demander l'identité — sous lui,
la portée manquerait précisément l'appelant qui paie en premier.

`on_message` est le hook qui enveloppe TOUT (fastmcp le pose en dernier, donc en plus
externe, dans `_dispatch_handler`) : `initialize`, `tools/list`, `tools/call`,
notifications comprises.

## Ce que fait la portée d'un échec de pré-résolution : elle replie, et elle le DIT

La pré-résolution est une **optimisation** : si elle échoue — base indisponible, chaîne
d'alias non résolvable (`AliasNonResolvable`), compte en pause (`CompteEnPause`),
exécuteur saturé — le cache reste vide et le premier vrai demandeur rejoue exactement le
chemin d'avant, au même endroit, avec la même exception. Lever ICI changerait la
destination du refus : ce middleware est plus externe que `ErrorEnvelopeMiddleware`,
donc son exception partirait SANS l'enveloppe d'erreur contractuelle
(`{code, retryable, hint}`) que l'agent attend, et le refus de compte en pause perdrait
le message que `AccountSuspendedMiddleware` sait formuler.

⚠️ **Mais le repli ne se fait pas en silence, et c'est le point.** Un repli muet ici
rendrait le service au chemin DANS la boucle — le gel même que ce module ferme — sans
que rien ne le dise : le correctif se désarmerait tout seul et on croirait qu'il tient.
D'où un cri à débit borné (un message par minute au plus, portant le NOMBRE
d'occurrences absorbées depuis le précédent). Ce n'est pas le repli qui est fautif,
c'est son silence.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastmcp.server.middleware import Middleware

from ..auth.hooks import identity_scope, prime_identity

logger = logging.getLogger(__name__)

# Le repli est le bon comportement ; son SILENCE ne l'était pas. Un correctif qui se
# désarme tout seul sans témoin, c'est le motif qu'on passe la nuit à traquer : la
# plateforme retomberait sur le chemin DANS la boucle — le gel même que ce lot ferme —
# et on croirait que le correctif tient. D'où un cri, mais à débit borné : si la base
# tombe, chaque message servi passe ici, et un cri par message noierait le journal au
# moment précis où on a besoin de le lire. On dit donc « c'est arrivé, N fois depuis
# la dernière fois », ce qui porte aussi l'AMPLEUR — qu'un cri par occurrence ne
# donnerait pas mieux. `logger.error` et non `warning` : la LoggingIntegration de
# Sentry relaie le premier, et une dégradation vers le chemin gelé doit être poussée,
# pas seulement écrite.
_INTERVALLE_CRI_S = 60.0
_dernier_cri = 0.0
_absorbes = 0


class IdentityScopeMiddleware(Middleware):
    """Ouvre la mémoire d'identité du message, et la garnit hors de la boucle."""

    async def on_message(self, context, call_next):
        with identity_scope():
            if getattr(context, "type", "request") == "request":
                try:
                    await asyncio.to_thread(prime_identity)
                except Exception:
                    # Le `logger.error` est écrit ICI, dans la branche, et non derrière
                    # l'aide qui compte : `scripts/lint_silences` lit l'AST du handler
                    # et ne suit aucun appel — une aide qui journalise parfaitement
                    # reste, pour lui, un silence (mesuré : `test_no_silent_except`
                    # rouge sur cette ligne tant que le cri vivait dans l'aide). Et il
                    # a raison de ne pas suivre : ce qu'il garde, c'est qu'on VOIE la
                    # branche parler à l'endroit où on la lit. L'aide ne garde donc que
                    # le débit, et rend le nombre à annoncer — 0 pour se taire.
                    absorbes = _degradation_a_annoncer()
                    if absorbes:
                        logger.error(
                            "pré-résolution d'identité en échec — le message est servi "
                            "par le chemin PARESSEUX, donc la canonicalisation du sub "
                            "repart DANS la boucle (jusqu'à 10 allers-retours PG par "
                            "appel, cf. docs/event-loop-perf.md mode n°5). "
                            "%s occurrence(s) depuis le dernier message.",
                            absorbes, exc_info=True)
            return await call_next(context)


def _degradation_a_annoncer() -> int:
    """Compte l'échec et rend le nombre d'occurrences à annoncer — `0` pour se taire.

    Le débit est borné à un message par `_INTERVALLE_CRI_S` : quand la base tombe,
    chaque message servi passe ici. Le compte rendu porte les occurrences absorbées
    depuis le cri précédent, pour que le silence intermédiaire ne coûte pas l'ampleur.

    Ne lève pas, et l'appelant ne lève pas non plus : une exception partie d'ici
    passerait au-dessus d'`ErrorEnvelopeMiddleware`, donc sans l'enveloppe
    contractuelle, et le refus de compte en pause perdrait le message
    qu'`AccountSuspendedMiddleware` sait formuler (cf. le docstring du module)."""
    global _dernier_cri, _absorbes
    _absorbes += 1
    maintenant = time.monotonic()
    if maintenant - _dernier_cri < _INTERVALLE_CRI_S:
        return 0
    absorbes, _absorbes, _dernier_cri = _absorbes, 0, maintenant
    return absorbes
