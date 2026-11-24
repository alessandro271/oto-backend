"""`ErrorEnvelopeMiddleware` — le contrat d'erreur uniforme rendu à l'agent."""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp.server.middleware import Middleware
from ..mcp_errors import McpError
from mcp.types import ErrorData
from starlette.concurrency import run_in_threadpool

from .. import error_taxonomy, session_org
from ..auth.hooks import current_user_sub_from_token
from ..connectors import connect_link
from ..connectors import health as connector_health

logger = logging.getLogger(__name__)


def _reachable_suffix(connector: str) -> str:
    """Suffixe « des clés existent à portée » pour l'enveloppe d'erreur. Sync (DB) —
    à appeler via `run_in_threadpool`. Réutilise le seam d'`access` (aucune règle
    d'accès recopiée ici) ; chaîne vide si rien à portée ou hors contexte."""
    from .. import access
    sub = current_user_sub_from_token()
    if not sub:
        return ""
    return access._reachable_hint(sub, access.current_org(sub), connector)


async def _parametres_de(context, exc) -> list:
    """Les paramètres de l'outil dont la SIGNATURE a refusé l'appel (oto#135) — ce qui
    permet au refus de dire le paramètre le plus proche d'une clé inconnue.

    L'outil est celui que NOMME l'erreur (`call[data_write]`), pas celui de la requête :
    derrière `oto_call`, c'est l'outil appelé qui a refusé. Lu dans le catalogue BRUT
    (outils masqués compris, même raison que `oto_tool_schema`), et seulement sur ce
    chemin d'erreur."""
    from fastmcp.server.providers.base import Provider
    nom = error_taxonomy.outil_de_signature(exc)
    ctx = getattr(context, "fastmcp_context", None)
    if not nom or ctx is None:
        return []
    for t in await Provider.list_tools(ctx.fastmcp):
        if t.name == nom:
            return list(((t.parameters or {}).get("properties") or {}))
    return []


def _detail(exc) -> dict:
    """Le premier `oto_detail` (dict) porté par la chaîne d'exceptions, sinon {}.

    Attribut DÉDIÉ, posé exprès par un outil sur son propre refus : jamais le
    `data` d'une `McpError` quelconque (une enveloppe déjà posée en porte un, et le
    recopier l'imbriquerait), jamais rien d'amont (anti-fuite)."""
    for e in error_taxonomy._chain(exc):
        d = getattr(e, "oto_detail", None)
        if isinstance(d, dict) and d:
            return d
    return {}


def _reconnexion_demandee(exc) -> bool:
    """Un module OAuth a dit que le consentement est mort (refresh refusé, révoqué) : ses
    exceptions s'appellent `<Fournisseur>ReauthRequired` (Google, Microsoft, HubSpot,
    LinkedIn Ads, Ubersuggest, jefacture, Instagram). Reconnues au NOM, comme ailleurs dans
    la taxonomie : l'enveloppe n'importe pas les modules de chaque connecteur."""
    return any(type(x).__name__.endswith("ReauthRequired")
               for x in error_taxonomy._chain(exc))


def _contrat_reconnexion(trace: Optional[dict]) -> dict:
    """The connection-refusal contract (`reason: reconnect`) for a key the upstream just
    refused, from the call's trace: which connector, under which key. Sync (DB) — via
    `run_in_threadpool`.

    Only a key someone in the org can set again gets an address: the caller's own key
    (`me`, in the org the key lives in), the org's (`org`) or a team's (`team:<id>`). A
    key lent by another person, the tenant's or the platform's: none — the contract leaves
    the host's key out, and the person cannot repair someone else's account."""
    trace = trace or {}
    connector = trace.get("resolved_connector")
    mode = trace.get("key_mode")
    entity_type, entity_id = (list(trace.get("credential_row") or ()) + [None, None])[:2]
    sub = current_user_sub_from_token()
    if not connector or not sub:
        return {}
    from .. import access
    if mode == "user":
        org_txt, _, titulaire = str(entity_id or "").partition(":")
        if entity_type != "member" or titulaire != sub or not org_txt.isdigit():
            return {}
        org, space = int(org_txt), "me"
    elif mode == "org":
        org, space = access.current_org(sub), "org"
    elif mode == "group" and entity_id:
        org, space = access.current_org(sub), connect_link.espace_d_equipe(entity_id)
    else:
        return {}
    return connect_link.details_du_refus("reconnect", connector, sub=sub, org=org,
                                         space=space)


class ErrorEnvelopeMiddleware(Middleware):
    """Contrat d'erreur uniforme rendu à l'agent (D2, oto-backend#124).

    Toute exception d'un tool est réécrite en `McpError` **scrubbée** (pas de
    stacktrace / route interne / id technique) portant `data.oto = {code, retryable,
    hint}` — l'agent peut alors DÉCIDER (retry / abandon / corriger l'input) au lieu
    de deviner sur un message brut. Les tools qui lèvent déjà une `McpError` curée
    voient leur message conservé (cf. `error_taxonomy.classify`).

    **Outermost** (ajouté AVANT `SentryToolErrorMiddleware`) : la chaîne s'exécute de
    l'extérieur vers l'intérieur, donc Sentry (plus interne) attrape l'exception
    d'ORIGINE en premier (vrai traceback capturé), la re-raise, et cette enveloppe la
    normalise EN DERNIER avant qu'elle ne quitte le serveur. Placer l'enveloppe plus
    interne masquerait le vrai traceback à Sentry.
    """

    async def on_call_tool(self, context, call_next):
        try:
            result = await call_next(context)
        except Exception as e:
            parametres: list = []
            if error_taxonomy._is_arg_validation_error(e):
                try:
                    parametres = await _parametres_de(context, e)
                # noqa: SILENT — un indice de plus : échouer à lire le catalogue ne doit jamais masquer le refus d'origine
                except Exception:  # noqa: BLE001
                    parametres = []
            info = error_taxonomy.classify(e, parametres)
            data = {"code": info.code, "retryable": info.retryable}
            hint = info.hint
            # Outil non monté = le PREMIER mur. Sans ça l'agent installe le
            # connecteur, rappelle, et se prend un SECOND mur (« aucun credential »)
            # qui seul portait l'info utile — alors qu'une clé existe peut-être à
            # portée (équipe dont il est membre, autre org). On remonte les deux
            # d'un coup. DB SYNC → threadpool obligatoire (serveur mono-loop), et
            # best-effort : un hoquet ici ne doit jamais masquer l'erreur d'origine.
            if info.connector:
                try:
                    hint = (hint or "") + await run_in_threadpool(
                        _reachable_suffix, info.connector)
                # noqa: SILENT — dette déclarée : le hint « une clé existe à portée » disparaît (#424, verdict C)
                except Exception:  # noqa: BLE001
                    pass
            # Crédits épuisés : la clé qui a servi CET appel passe au rouge sur sa
            # fiche (« recharge »), sauf clé plateforme/tenant — garde de portée de
            # `connectors.health`. Le relevé est encore posé ici : `CallContext`
            # (plus externe) ne le défait qu'après nous.
            if info.code == "quota_exhausted":
                trace = session_org.current_call_trace()
                fournisseur = (trace or {}).get("resolved_connector")
                if fournisseur:
                    hint = f"{hint} — chez `{fournisseur}`"
                await connector_health.suivre_appel(trace, info.message)
            # The connection-refusal contract (`connectors/connect_link`): the fields a
            # refusal posed itself (connector cut, no key) — `data.oto.url` is the page
            # where the person does the one gesture left.
            message = info.message
            connexion = connect_link.lu(e)
            # Clé refusée par l'amont (401, ou refus que son connecteur déclare comme
            # celui de la clé) : même marque au rouge, verdict « repose la clé » — et
            # l'adresse où la reposer (`reason: reconnect`), le message finissant par elle.
            rejet = (info.code != "quota_exhausted"
                     and error_taxonomy.credential_rejected_in_chain(e))
            if rejet:
                await connector_health.suivre_appel(
                    session_org.current_call_trace(), None, rejet=info.message)
            # Un consentement mort (`<X>ReauthRequired` : son module a déjà marqué sa ligne)
            # mène au même geste, sans seconde marque.
            if rejet or _reconnexion_demandee(e):
                if not connexion:
                    try:
                        connexion = await run_in_threadpool(
                            _contrat_reconnexion, session_org.current_call_trace())
                    # noqa: SILENT — journalisé : l'adresse manque, le refus d'origine reste servi
                    except Exception:  # noqa: BLE001
                        logger.exception("reconnect contract not built")
                        connexion = {}
                    message += connect_link.phrase(connexion)
            data.update(connexion)
            if hint:
                data["hint"] = hint
            # Faits STRUCTURÉS qu'un outil a posés sur son refus (`oto_detail`, un
            # dict à nous — ex. hubspot : le scope à ajouter). Sans ce relais, ils
            # mouraient ici : l'enveloppe réécrit tout et ne gardait que le message.
            detail = _detail(e)
            if detail:
                data["detail"] = detail
            raise McpError(ErrorData(
                code=error_taxonomy.jsonrpc_code(info),
                message=message,
                data={"oto": data},
            )) from e
        # Succès : une marque « crédits épuisés » de la clé servie est levée (une seule
        # écriture conditionnelle par clé et par process — cf. `connectors.health`).
        await connector_health.suivre_appel(session_org.current_call_trace(), None)
        return result
