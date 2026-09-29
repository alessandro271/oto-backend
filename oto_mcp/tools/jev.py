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

⚠️ **La clé est celle du TENANT, jamais celle d'une org ni de la plateforme** (cf.
`providers/jev.py`) : un administrateur du tenant la dépose pour toutes ses orgs, et
une clé posée plus près de l'appelant (org, équipe, personne) est refusée à l'usage
en le disant — elle masque celle du tenant dans la cascade.

⚠️ L'état part chez un TIERS (OpenRouter, qui le passe à TypeSafe) : le texte servi
le dit, pour que l'agent n'y mette que les champs utiles au jugement.

Facturation : l'amont facture l'ENTRÉE seule (sortie gratuite), et chaque réponse
porte `usage.cost`, le coût réel en dollars. C'est ce coût — en micro-dollars — qui
part au relevé (`note_call_trace(quantity=…)`), pas un nombre d'appels : le prix
suit alors la dépense réelle au lieu d'un forfait qui vieillit. Même usage de
`quantity` que `serper` (les crédits déduits par le fournisseur).
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Optional

import requests
from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, credentials_store, session_org
from ..access.resolve import CredentialUnavailable
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

#: Le seul barreau de la cascade que cet outil accepte : la clé du TENANT (décision du
#: 28/09/2026 — aucune clé plateforme, le tenant qui apporte sa clé répond de la
#: dépense et de la sous-traitance). Un dépôt plus proche de l'appelant (org, équipe,
#: personne) gagne la cascade AVANT le tenant : il est refusé en disant qu'il masque la
#: clé du tenant et qui peut le retirer (cf. `_client`).
RANGS_SERVIS = ("tenant",)

#: Qui peut retirer une clé qui masque celle du tenant, par barreau.
QUI_RETIRE = {"org": "un administrateur de l'org",
              "group": "un administrateur de l'équipe",
              "user": "son titulaire, sur sa page compte"}

#: Combien d'états au plus dans un `jev_items`. Mesuré le 28/09/2026 : 50 appels en
#: vol rendent 50/50 en une seconde, sans un seul 429 ; 200 états à 10 en vol tiennent
#: dans la fenêtre de départ ci-dessous.
MAX_ITEMS = 200
#: Appels en vol par défaut — volontairement en dessous du débit mesuré : ce sont des
#: threads du pool du serveur, et un outil ne se sert pas tout seul.
PARALLELE_DEFAUT = 10
PARALLELE_MAX = 50

#: Taille maximale d'UN état, en octets de son JSON (UTF-8). 16 Ko, c'est ~4 000
#: jetons : très au-dessus d'une ligne de table ou d'un profil (ce que jev juge),
#: très en dessous du contexte de 32 000 jetons — et c'est la borne qui plafonne la
#: dépense d'un appel (200 états × 16 Ko au pire), l'entrée seule étant facturée.
#: Un état plus gros est un document entier : c'est le signal d'un usage à tort.
MAX_ETAT_OCTETS = 16_000

#: Délai de LECTURE d'une décision (la connexion garde les 10 s du client). Une
#: décision répond en ~0,5 s : 15 s, c'est un amont en difficulté, pas une décision lente.
LECTURE_S = 15
#: Plafond dur d'un appel sur le chemin REST (`capabilities/tools_me.py`,
#: `asyncio.wait_for(..., timeout=45)`), même borne que `clay`.
REST_CALL_LIMIT_S = 45.0
#: Fenêtre de DÉPART d'un lot : aucune décision ne part après elle, et une décision
#: partie juste avant peut encore durer connexion + lecture (10 + 15 s). D'où : plafond
#: REST − la pire décision − une marge, soit un lot qui rend TOUJOURS sous ~40 s, avec
#: les états non partis à rejouer, au lieu d'un appel coupé sans reçu.
LOT_FENETRE_S = REST_CALL_LIMIT_S - (10 + LECTURE_S) - 5.0


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        return (f"OpenRouter a rejeté la clé (HTTP {status}) — la clé Jev du tenant "
                "est invalide ou révoquée ; c'est un administrateur du tenant qui la "
                "repose.")
    if status == 402:
        return ("Crédits OpenRouter épuisés (402) — la clé du tenant n'a plus de "
                "solde ; c'est un administrateur du tenant qui la recharge.")
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


def _verify(fields: dict, config: dict | None = None,  # noqa: ARG001
            instance: tuple | None = None) -> None:
    """Sonde « tester la connexion » : la plus petite décision possible — un état d'un
    mot, une question oui/non. Une décision coûte quelques micro-dollars ; c'est
    l'appel authentifié le moins cher que cette API propose.

    ⚠️ Une clé posée ailleurs que sur le TENANT échoue ici SANS appel : elle est
    refusée à l'usage (`RANGS_SERVIS`), une carte verte mentirait. `instance` est la
    ligne réellement sondée ; absente (vérification avant dépôt, grant sans ligne),
    la sonde ne peut pas juger du barreau et teste la clé seule."""
    if instance is not None and instance[0] != credentials_store.TENANT:
        raise ValueError(
            f"une clé `jev` posée au niveau « {instance[0]} » n'est pas servie : jev "
            "ne tourne que sur la clé du TENANT, et celle-ci la masquerait.")
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

    def _client(units: int = 1) -> JevClient:
        """Le client sur la clé du tenant. `units` = le nombre de décisions de l'appel,
        vérifié d'avance contre le quota de la clé (`resolve_credential`).

        ⚠️ **Le barreau gagnant est VÉRIFIÉ, pas seulement lu.** La cascade rendrait
        volontiers une clé posée par une org ou une personne ; cet outil ne tourne que
        sur la clé du tenant (`providers/jev.py`), et une clé d'org servie en silence
        ferait deux choses fausses à la fois — un travail payé par quelqu'un qui ne
        l'a pas voulu, et un usage que le tenant ne verrait pas.

        ⚠️ Sans aucune clé, le refus générique de la cascade propose de « poser ta
        propre clé » — exactement le geste refusé ici. Il est remplacé par la seule
        voie qui existe."""
        try:
            rc = access.resolve_credential("jev", want="auto", units=units)
        except CredentialUnavailable as e:
            raise CredentialUnavailable(ErrorData(
                code=INVALID_PARAMS,
                message=("Aucune clé `jev` servie pour ton org : jev tourne sur la clé "
                         "que ton TENANT dépose (un de ses administrateurs la pose une "
                         "fois pour toutes ses orgs). oto ne fournit pas de clé "
                         "plateforme `jev`, et une clé posée par une org, une équipe "
                         "ou une personne n'est pas servie."))) from e
        if rc.mode not in RANGS_SERVIS:
            qui = QUI_RETIRE.get(rc.mode, "celui qui l'a posée")
            raise _bad(
                f"Une clé `jev` posée au niveau « {rc.mode} » masque celle du TENANT : "
                "jev ne tourne que sur la clé du tenant, et cette clé-là n'est pas "
                f"servie. Pour décider sur la clé du tenant, {qui} doit la retirer "
                "(carte du connecteur jev).")
        return JevClient(api_key=rc.key)

    def _etat_borne(state, ou: str) -> None:
        """Refuse un état au-delà de `MAX_ETAT_OCTETS` AVANT tout appel."""
        taille = len(json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"))
        if taille > MAX_ETAT_OCTETS:
            raise _bad(
                f"{ou} : l'état fait {taille} octets, le maximum est "
                f"{MAX_ETAT_OCTETS} — n'envoie que les champs qui servent au "
                "jugement, pas la fiche ou le document entier.")

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

    def _releve(cout_total: float) -> None:
        """Le relevé d'un appel : sa dépense RÉELLE, en micro-dollars.

        ⚠️ Sans `quantity`, un `jev_items` de deux cents décisions se relèverait comme
        un appel unique — et le prix ne suivrait plus rien."""
        u = _microdollars(cout_total)
        if u:
            session_org.note_call_trace(quantity=u)

    @mcp.tool()
    def jev_ask(state: dict, questions: dict, model: Optional[str] = None) -> dict:
        """Ask Jev typed questions about one state and get answers with probabilities.

        Jev is not a chat model: it returns a typed answer per
        question and nothing else — no prose, no justification, no tool calls. It pays
        off on BATCHES — many rows or profiles to triage with the same rubric (see
        `jev_items`). A single case you can judge yourself does not need Jev.

        ⚠️ The state is sent to a third party (OpenRouter, which passes it to TypeSafe):
        put in it only the fields the judgement needs, never a whole record or
        document.

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
                "company": …}`), at most 16 KB of JSON. Send the fields that matter,
                not the whole record: the state is billed per token.
            questions: `{name: {type, instructions, criteria}}`.
                `noul` — criteria `{"true": …, "false": …}`, both described;
                `choice` — one entry per option, each described;
                `score` — an ORDERED list of levels, low to high.
                `criteria` is required.
            model: another model id (default: the pinned dated snapshot).
        """
        _etat_borne(state, "`state`")
        client = _client()
        with _upstream():
            r = client.decide(state, questions, model=model, timeout=LECTURE_S)
        _releve((r.get("usage") or {}).get("cost"))
        return {"model": r.get("model"), "answers": r.get("answers") or {},
                "usage": r.get("usage") or {}}

    @mcp.tool()
    def jev_items(items: list, questions: dict, model: Optional[str] = None,
                  parallel: Optional[int] = None) -> dict:
        """Ask Jev the SAME questions about many states in one call — nothing is written.

        The shape for "decide, then act": you have just read a page of rows or a list
        of profiles to triage with the same rubric, and you want a verdict per item
        before writing them, enriching them, or spending anything on them. Each item
        is judged on its own — one item's answer never depends on another's.

        ⚠️ Each state is sent to a third party (OpenRouter, which passes it to
        TypeSafe): put in it only the fields the judgement needs.

        Returns — `{model, answers, usage, failed, retry}`:
            `answers` — one entry per item, in the ORDER SENT, each
            `{index, answers: {…}}` exactly as `jev_ask` returns them, or
            `{index, error}` for an item without an answer (a state the upstream
            refused, a transient failure, or not sent in time). A failed item never
            becomes a false verdict.
            `usage` — `{decided, failed, input_tokens, cost}` for the whole call.
            `failed` — the number of items without an answer.
            `retry` — the indexes that were NOT SENT because the call ran out of time
            (about 40 s): send exactly those again. `[]` when everything was sent.

        A key problem (invalid key, no credits left) aborts the whole call rather than
        turning into N identical item errors; the answers already given are still
        billed, and the error says how many.

        Args:
            items: the states to judge, at most 200 per call and 16 KB of JSON each.
                Either the raw states, or `{"key": <your id>, "state": {…}}` to get
                your own id back beside each answer (`key` is echoed, never sent).
            questions: the rubric, same shape and same rules as `jev_ask`.
            model: another model id (default: the pinned dated snapshot).
            parallel: how many items in flight (default 10, max 50). Raise it for
                a big batch that must finish inside one call.
        """
        if not isinstance(items, list) or not items:
            raise _bad("`items` : au moins un état à juger est attendu.")
        if len(items) > MAX_ITEMS:
            raise _bad(f"`items` : {len(items)} états, le maximum est {MAX_ITEMS} par "
                       "appel — découpe en pages et rappelle.")
        try:
            fil = max(1, min(int(parallel or PARALLELE_DEFAUT), PARALLELE_MAX))
        except (TypeError, ValueError):
            raise _bad(f"`parallel` : un entier entre 1 et {PARALLELE_MAX} est attendu, "
                       f"pas {parallel!r}.")

        def _etat(i, x) -> tuple[Optional[str], dict]:
            if isinstance(x, dict) and "state" in x and isinstance(x["state"], dict):
                cle = x.get("key")
                cle, state = (str(cle) if cle is not None else None), x["state"]
            elif isinstance(x, dict):
                cle, state = None, x
            else:
                raise _bad("chaque élément de `items` est un objet (l'état), "
                           "ou `{key, state}`.")
            _etat_borne(state, f"`items[{i}]`")
            return cle, state

        paires = [_etat(i, x) for i, x in enumerate(items)]
        client = _client(units=len(paires))
        # La grille est jugée UNE fois, pas une fois par état : une grille fautive
        # n'a pas à coûter deux cents refus identiques.
        with _upstream():
            client.check_questions(questions)

        # Aucune décision ne PART après la fenêtre, ni après un arrêt du lot : une
        # décision déjà partie finit (un appel HTTP synchrone ne s'annule pas), et
        # son coût est relevé.
        fin_depart = time.monotonic() + LOT_FENETRE_S
        arret = threading.Event()

        def _une(i: int, cle: Optional[str], state: dict) -> dict:
            if arret.is_set() or time.monotonic() >= fin_depart:
                return {"index": i, "key": cle, "_non_parti": True,
                        "error": "not sent (time budget or batch stopped) — send it again"}
            try:
                r = client.decide(state, questions, model=model, timeout=LECTURE_S)
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

        res: list[dict] = []
        panne: Optional[Exception] = None
        try:
            with ThreadPoolExecutor(max_workers=fil) as ex:
                futurs = [ex.submit(_une, i, cle, state)
                          for i, (cle, state) in enumerate(paires)]
                for f in as_completed(futurs):
                    try:
                        res.append(f.result())
                    except Exception as e:  # noqa: SILENT — retenue, relevée plus bas après le relevé
                        # Clé, solde, ou exception inattendue sortie d'un fil : le
                        # lot s'arrête, mais ce qui est déjà décidé reste PAYÉ.
                        if panne is None:
                            panne = e
                            arret.set()
                            for autre in futurs:
                                autre.cancel()
        finally:
            # Ce qui est parti chez l'amont compte, même si le lot s'arrête en route.
            cout = sum((r.get("_usage") or {}).get("cost") or 0 for r in res)
            _releve(cout)

        decides = sum(1 for r in res if "answers" in r)
        if panne is not None:
            deja = (f" {decides} réponse(s) déjà rendue(s) et relevée(s) avant l'arrêt."
                    if decides else " Aucune réponse n'avait encore été rendue.")
            if isinstance(panne, UpstreamHTTPError):
                raise _bad(_upstream_message(panne) + deja) from panne
            raise panne

        res.sort(key=lambda r: r["index"])
        jetons = sum((r.get("_usage") or {}).get("input_tokens") or 0 for r in res)
        servi = next((r.get("_model") for r in res if r.get("_model")), None)
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
                "retry": [r["index"] for r in res if r.get("_non_parti")],
                "usage": {"decided": len(rendus) - rates, "failed": rates,
                          "input_tokens": jetons, "cost": cout}}
