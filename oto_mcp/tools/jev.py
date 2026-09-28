"""Jev — une décision TYPÉE au lieu d'un tour de modèle (TypeSafe, via OpenRouter).

Wrappe `oto.tools.jev.client.JevClient`. On donne un **état** et des **questions** ;
Jev rend une réponse typée par question, avec sa probabilité :
- `noul` → la probabilité que la condition tienne ;
- `choice` → l'option retenue, la probabilité de chacune, la confiance ;
- `score` → la position sur une échelle ordonnée, et sa légende.

Aucun texte, aucune justification, aucun appel d'outil : Jev ne remplace pas l'agent
qui mène le travail, il remplace le geste « je demande au modèle et je parse sa
réponse » — qualifier une ligne, trier un message, juger si une donnée tient.

Deux gestes :
- `jev_ask` : UN état, la grille entière en un appel (les questions d'un même appel
  sont répondues en parallèle et ne se voient pas l'une l'autre) ;
- `jev_items` : N états, la MÊME grille, en un appel — la forme utile quand on vient
  de lire une page de lignes ou une liste de profils, et qu'on veut trancher avant
  d'écrire ou de payer l'étape suivante.

⚠️ **La clé est celle de la PLATEFORME, jamais celle d'une org** (cf.
`providers/jev.py`) : elle se dépose au palier TENANT, et une clé posée sur une org
est refusée à l'usage en le disant.

Facturation : l'amont facture l'ENTRÉE seule (sortie gratuite), et chaque réponse
porte `usage.cost`, le coût réel en dollars. C'est ce coût — en micro-dollars — qui
part au relevé (`note_call_trace(quantity=…)`), pas un nombre d'appels : le prix
suit alors la dépense réelle au lieu d'un forfait qui vieillit. Même usage de
`quantity` que `serper` (les crédits déduits par le fournisseur).
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Optional

import requests
from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

#: Les seuls barreaux de la cascade que cet outil accepte : la clé du TENANT (celle
#: que la plateforme dépose pour ses orgs) et le grant PLATEFORME. Un dépôt d'org ou
#: de personne est ignoré — et le dire est la moitié du travail (cf. `_client`).
RANGS_SERVIS = ("tenant", "platform")

#: Combien d'états au plus dans un `jev_items`. Mesuré le 28/09/2026 : 50 appels en
#: vol rendent 50/50 en une seconde, sans un seul 429 ; 200 états à 10 en vol tiennent
#: largement sous le plafond de 45 s de la face REST.
MAX_ITEMS = 200
#: Appels en vol par défaut — volontairement en dessous du débit mesuré : ce sont des
#: threads du pool du serveur, et un outil ne se sert pas tout seul.
PARALLELE_DEFAUT = 10
PARALLELE_MAX = 50


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        return (f"OpenRouter a rejeté la clé (HTTP {status}) — la clé Jev de la "
                "plateforme est invalide ou révoquée ; c'est un administrateur du "
                "tenant qui la repose.")
    if status == 402:
        return ("Crédits OpenRouter épuisés (402) — la clé de la plateforme n'a plus "
                "de solde. Aucune décision n'a été prise ; rien n'est facturé.")
    if status == 429:
        return "Jev : trop de requêtes (429) — réessaie dans un instant."
    if status == 400:
        # Le DIRE de l'amont, entier : il nomme la question fautive ou l'état trop
        # long (`max_tokens_exceeded`, au-delà de 32 000 jetons), et le reformuler
        # ferait perdre exactement ce qui permet de corriger.
        return f"Jev a refusé la requête (400) — {e.body}"
    if status in (500, 502, 503, 504):
        return f"Jev est momentanément indisponible (HTTP {status}) — réessaie plus tard."
    return f"Jev a refusé la requête (HTTP {status}): {e.body}"


def _microdollars(cout: Optional[float]) -> int:
    """Le coût d'un appel en micro-dollars, ARRONDI AU SUPÉRIEUR.

    C'est l'unité du relevé (`quantity`) : un appel qui a coûté quelque chose ne doit
    jamais se relever à zéro. Un coût absent (l'amont ne l'a pas déclaré) rend 0 — on
    ne facture pas ce qu'on n'a pas mesuré.

    ⚠️ **Le `round` avant le plafond n'est pas cosmétique** : la somme des coûts d'un
    lot est une addition de flottants, et `3 × 1,0e-05` vaut `3,0000000000000004e-05`
    en binaire — soit 30,000000000000004 µ$, que `ceil` seul relèverait à **31**. Un
    micro-dollar inventé à chaque lot, toujours dans le même sens, sur une marge qui
    se compte en multiples du coût réel : la décimale est ici une question de
    facture, pas de présentation."""
    return math.ceil(round(float(cout) * 1e6, 6)) if cout else 0


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : la plus petite décision possible — un état d'un
    mot, une question oui/non. Une décision coûte quelques micro-dollars ; c'est
    l'appel authentifié le moins cher que cette API propose."""
    from oto.tools.jev.client import JevClient
    JevClient(api_key=fields["key"]).decide(
        {"mot": "test"},
        {"ok": {"type": "noul", "instructions": "Ce mot est-il « test » ?",
                "criteria": {"true": "c'est le mot test", "false": "c'est un autre mot"}}},
        timeout=20)


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.jev.client import JevClient

    connector_verify.register("jev", _verify)

    def _client() -> tuple[JevClient, bool]:
        """La clé servie, et si elle vient du grant plateforme.

        ⚠️ **Le barreau gagnant est VÉRIFIÉ, pas seulement lu.** La cascade rendrait
        volontiers une clé posée par une org ou une personne ; cet outil ne tourne que
        sur la clé de la plateforme (`providers/jev.py`), et une clé d'org servie en
        silence ferait deux choses fausses à la fois — un travail payé par quelqu'un
        qui ne l'a pas voulu, et un usage qui n'apparaît dans aucun relevé."""
        rc = access.resolve_credential("jev", want="auto")
        if rc.mode not in RANGS_SERVIS:
            raise _bad(
                f"Jev tourne sur la clé de la plateforme, pas sur une clé déposée au "
                f"niveau « {rc.mode} » : cette clé-là n'est pas servie. La clé Jev se "
                "dépose sur le TENANT, par un de ses administrateurs.")
        return JevClient(api_key=rc.key), rc.is_platform

    @contextmanager
    def _upstream():
        """Traduit un refus de l'amont en erreur d'outil actionnable."""
        try:
            yield
        except ValueError as e:
            # La garde de grille du client (type inconnu, `criteria` absent) : elle
            # nomme la question fautive, c'est déjà le bon message.
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise _bad(f"Jev injoignable (réseau/timeout) — réessaie plus tard. {e}")

    def _releve(cout_total: float, is_platform: bool) -> None:
        """Le relevé d'un appel : sa dépense RÉELLE, en micro-dollars.

        ⚠️ Sans `quantity`, un `jev_items` de deux cents décisions se relèverait comme
        un appel unique — et le prix ne suivrait plus rien."""
        u = _microdollars(cout_total)
        if u:
            session_org.note_call_trace(quantity=u)
            if is_platform:
                access.record_platform_usage("jev", u)

    @mcp.tool()
    def jev_ask(state: dict, questions: dict, model: Optional[str] = None) -> dict:
        """Ask Jev typed questions about one state and get answers with probabilities.

        Jev is a decision model, not a chat model: it returns a typed answer per
        question and nothing else — no prose, no justification, no tool calls. Use it
        wherever you would otherwise spend a model turn to produce a label: qualify a
        row, route a ticket, check whether a draft is supported by its source.

        Put the WHOLE rubric in one call: the questions of one call are answered in
        parallel, each extra question costs about 48 input tokens, and the state is
        charged once. They cannot see each other's answers, so a question that depends
        on another's answer needs a second call.

        Returns — `{model, answers, usage}`:
            `answers[name]` is `{type: "noul", noul: 0.96}`, or
            `{type: "choice", choice, probabilities, confidence}`, or
            `{type: "score", score, probabilities, legend, confidence}`.
            `model` names the exact snapshot that answered — store it next to the
            answer, it is what makes a threshold reproducible.
            `usage` carries `input_tokens`, `output_tokens` (free) and `cost` in USD.

        A probability near 0.5 means "as likely as not", never "medium": pick two
        thresholds and leave the band between them to a human or to a stronger model.

        Args:
            state: what Jev judges — a flat or nested object (`{"title": …,
                "company": …}`). Send the fields that matter, not the whole record:
                the state is billed per token and the context stops at 32 000.
            questions: `{name: {type, instructions, criteria}}`.
                `noul` — criteria `{"true": …, "false": …}`, both described;
                `choice` — one entry per option, each described;
                `score` — an ORDERED list of levels, low to high.
                `criteria` is required: without it the upstream still answers, with a
                probability that means nothing.
            model: another model id (default: the pinned dated snapshot).
        """
        client, is_platform = _client()
        with _upstream():
            r = client.decide(state, questions, model=model)
        _releve((r.get("usage") or {}).get("cost"), is_platform)
        return {"model": r.get("model"), "answers": r.get("answers") or {},
                "usage": r.get("usage") or {}}

    @mcp.tool()
    def jev_items(items: list, questions: dict, model: Optional[str] = None,
                  parallel: Optional[int] = None) -> dict:
        """Ask Jev the SAME questions about many states in one call — nothing is written.

        The shape for "decide, then act": you have just read a page of rows or a list
        of profiles, and you want a verdict per item before writing them, enriching
        them, or spending anything on them. Each item is judged on its own — one item's
        answer never depends on another's.

        Returns — `{model, answers, usage, failed}`:
            `answers` — one entry per item, in the ORDER SENT, each
            `{index, answers: {…}}` exactly as `jev_ask` returns them, or
            `{index, error}` for an item the upstream refused (a state too long, a
            transient failure). A failed item never becomes a false verdict.
            `usage` — `{decided, failed, input_tokens, cost}` for the whole call.
            `failed` — the number of items without an answer, so a caller that only
            reads the summary still sees them.

        A key problem (invalid key, no credits left) aborts the whole call rather than
        turning into N identical item errors.

        Args:
            items: the states to judge, at most 200 per call. Either the raw states,
                or `{"key": <your id>, "state": {…}}` to get your own id back beside
                each answer (`key` is echoed, never sent to the model).
            questions: the rubric, same shape and same rules as `jev_ask`.
            model: another model id (default: the pinned dated snapshot).
            parallel: how many decisions in flight (default 10, max 50). Raise it for
                a big batch that must finish inside one call; the upstream held 50 in
                flight without a single rate limit on 2026-09-28.
        """
        if not isinstance(items, list) or not items:
            raise _bad("`items` : au moins un état à juger est attendu.")
        if len(items) > MAX_ITEMS:
            raise _bad(f"`items` : {len(items)} états, le maximum est {MAX_ITEMS} par "
                       "appel — découpe en pages et rappelle.")
        client, is_platform = _client()
        # La grille est jugée UNE fois, pas une fois par état : une grille fautive
        # n'a pas à coûter deux cents refus identiques.
        with _upstream():
            client.check_questions(questions)

        fil = max(1, min(int(parallel or PARALLELE_DEFAUT), PARALLELE_MAX))

        def _etat(x) -> tuple[Optional[str], dict]:
            if isinstance(x, dict) and "state" in x and isinstance(x["state"], dict):
                cle = x.get("key")
                return (str(cle) if cle is not None else None), x["state"]
            if not isinstance(x, dict):
                raise _bad("chaque élément de `items` est un objet (l'état), "
                           "ou `{key, state}`.")
            return None, x

        paires = [_etat(x) for x in items]

        def _une(i_paire):
            i, (cle, state) = i_paire
            try:
                r = client.decide(state, questions, model=model)
                return {"index": i, "key": cle, "answers": r.get("answers") or {},
                        "_usage": r.get("usage") or {}, "_model": r.get("model")}
            except UpstreamHTTPError as e:
                # ⚠️ Un problème de CLÉ ou de SOLDE n'est pas l'affaire d'un état : il
                # remonte et arrête le lot, au lieu de se répéter deux cents fois.
                if e.status_code in (401, 402, 403):
                    raise
                return {"index": i, "key": cle, "error": _upstream_message(e)}
            except (requests.ConnectionError, requests.Timeout) as e:
                return {"index": i, "key": cle, "error": f"Jev injoignable — {e}"}

        with _upstream():
            with ThreadPoolExecutor(max_workers=fil) as ex:
                res = list(ex.map(_une, enumerate(paires)))

        cout = sum((r.get("_usage") or {}).get("cost") or 0 for r in res)
        jetons = sum((r.get("_usage") or {}).get("input_tokens") or 0 for r in res)
        servi = next((r.get("_model") for r in res if r.get("_model")), None)
        _releve(cout, is_platform)
        rendus = []
        for r in res:
            ligne = {"index": r["index"]}
            if r.get("key") is not None:
                ligne["key"] = r["key"]
            if "error" in r:
                ligne["error"] = r["error"]
            else:
                ligne["answers"] = r["answers"]
            rendus.append(ligne)
        rates = sum(1 for r in rendus if "error" in r)
        return {"model": servi, "answers": rendus, "failed": rates,
                "usage": {"decided": len(rendus) - rates, "failed": rates,
                          "input_tokens": jetons, "cost": cout}}
