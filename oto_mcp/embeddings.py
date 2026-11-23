"""Embeddings de texte (lot 3, recherche sémantique V2) — client Mistral.

`mistral-embed` (dim 1024, ~0,10 €/M tokens) — pour un
import sans re-embedding le jour du sunset. Deux surfaces :

- **sync `embed_requete`** : une requête pour le WORKER d'indexation (`embed_worker`),
  appelée hors event loop (threadpool) → jamais de blocage de la boucle mono-loop ;
  elle rend les tokens facturés, que le worker compte contre son plafond journalier ;
- **async `embed_query`** : un seul texte pour le CHEMIN REQUÊTE (`oto_search`), awaité
  par le handler async → pas de blocage non plus.

Gaté sur `MISTRAL_API_KEY` : sans clé, `enabled()` = False et tout le sémantique est
inerte (la recherche reste lexicale). Aucune dépendance oto-core (interne backend).
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_URL = "https://api.mistral.ai/v1/embeddings"
MODEL = "mistral-embed"
DIM = 1024
# **La borne d'un morceau est en OCTETS UTF-8, pas en caractères.** `mistral-embed`
# accepte 8k tokens par texte (contexte « 8k », docs.mistral.ai/models/mistral-embed-23-12).
# Son tokenizer (SentencePiece de Mistral, avec repli sur les octets) ne produit jamais
# plus d'un token par octet : `octets ≤ 6 000` garantit `tokens ≤ 6 000 + quelques
# jetons spéciaux`, sous les 8 192, quel que soit le texte. L'ancienne borne (16 000
# caractères, « ~4 caractères par token ») ne tenait que pour de la prose latine : du
# code, du JSON, des URL, des identifiants ou un texte non latin dépassent un token par
# 4 caractères, et le morceau partait en 400.
MAX_OCTETS_MORCEAU = 6000
# Budget d'UNE requête, tous textes confondus : le fournisseur refuse au-delà d'un total
# (« Too many tokens overall, split into more batches ») qu'il ne documente pas. Borné à
# la limite par texte, qu'il accepte par construction, sous la même garantie octets ≥
# tokens. Le coût ne dépend que des tokens : plus de requêtes ne coûte rien de plus.
MAX_OCTETS_REQUETE = 8000


def _octets(t: str) -> int:
    return len(t.encode("utf-8"))


def _tronque_octets(t: str, n: int) -> str:
    """Le plus long préfixe de `t` qui tient en `n` octets, sans couper un caractère."""
    return t.encode("utf-8")[:n].decode("utf-8", errors="ignore")


def _cap(text: str) -> str:
    """Un texte de requête (`embed_query`) borné comme un morceau. Vide → espace (l'API
    rejette une chaîne vide)."""
    t = (text or "").strip()
    return _tronque_octets(t, MAX_OCTETS_MORCEAU) if t else " "


def _coupe(t: str, n: int) -> list[str]:
    """Découpe un bloc trop gros en tranches de ≤ `n` octets, sur un espace quand il y en
    a un dans la seconde moitié de la tranche, sinon net (sans couper un caractère)."""
    out: list[str] = []
    while _octets(t) > n:
        tete = _tronque_octets(t, n)
        sp = tete.rfind(" ")
        if sp > len(tete) // 2:
            tete = tete[:sp]
        out.append(tete.strip())
        t = t[len(tete):].strip()
    if t:
        out.append(t)
    return [c for c in out if c]


def chunks(text: str, size: int = MAX_OCTETS_MORCEAU) -> list[str]:
    """Découpe un texte en morceaux de ≤ `size` octets UTF-8 (une page longue en
    PLUSIEURS vecteurs → sa fin reste recherchable, oto/#6 C). Les paragraphes (lignes
    vides) sont regroupés tant qu'ils tiennent ; un paragraphe trop gros est coupé sur
    un espace. Une retouche dans un paragraphe laisse intacts les morceaux qui la
    précèdent : leur empreinte ne bouge pas, ils ne sont pas ré-embarqués. Vide → []."""
    t = (text or "").strip()
    if not t:
        return []
    if _octets(t) <= size:
        return [t]
    blocs: list[str] = []
    for p in t.split("\n\n"):
        p = p.strip()
        if p:
            blocs.extend(_coupe(p, size))
    out: list[str] = []
    cur = ""
    for b in blocs:
        if cur and _octets(cur) + 2 + _octets(b) > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


# Octobre 2026 : 59 M de tokens sur le mois, soit ~2 M par jour. Le défaut laisse 2,5 fois
# cette moyenne à une journée chargée (la reprise d'une file accumulée : 11,7 Mo de pages
# en attente le 23/11, ~3 M tokens), et borne la pire journée à 5 M tokens, 0,50 $ au
# tarif de `mistral-embed` (0,10 $ le million).
PLAFOND_DEFAUT = 5_000_000


class ReglagePlafondInvalide(ValueError):
    pass


def plafond_jour() -> int:
    """`OTO_EMBED_PLAFOND_TOKENS_JOUR` : tokens d'embedding FACTURÉS par jour (UTC) et
    par instance, indexation et requêtes de recherche confondues. Absent ou vide :
    `PLAFOND_DEFAUT`. `0` : rien n'est embarqué. Toute autre valeur qu'un entier positif
    LÈVE."""
    brut = os.environ.get("OTO_EMBED_PLAFOND_TOKENS_JOUR")
    if brut is None or brut.strip() == "":
        return PLAFOND_DEFAUT
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = -1
    if valeur < 0:
        raise ReglagePlafondInvalide(
            f"OTO_EMBED_PLAFOND_TOKENS_JOUR={brut!r} illisible : un entier positif "
            "(tokens par jour), `0` n'embarque rien.")
    return valeur


def enabled() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}",
            "Content-Type": "application/json"}


class EchecEmbedding(Exception):
    """Une requête d'embedding refusée ou perdue. `definitif` : le fournisseur refuse
    ces textes-là (400, 413, 422) — les renvoyer tels quels échouera encore. Sinon
    (limite de débit, erreur serveur, réseau, clé refusée), la cause n'est pas le texte."""

    def __init__(self, raison: str, *, definitif: bool):
        super().__init__(raison)
        self.raison = raison
        self.definitif = definitif


_DEFINITIFS = {400, 413, 422}


def lots(texts: list[str]):
    """Regroupe des morceaux (chacun ≤ `MAX_OCTETS_MORCEAU`) en requêtes de ≤
    `MAX_OCTETS_REQUETE` octets, ordre préservé."""
    cur: list[str] = []
    size = 0
    for t in texts:
        n = _octets(t)
        if cur and size + n > MAX_OCTETS_REQUETE:
            yield cur
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        yield cur


def borne_tokens(texts: list[str]) -> int:
    """Majorant des tokens d'une requête : un par octet, plus deux jetons spéciaux par
    texte. Sert au plafond journalier AVANT l'appel."""
    return sum(_octets(t) + 2 for t in texts)


def embed_requete(texts: list[str], *, timeout: float = 30.0) -> tuple[list[list[float]], int]:
    """UNE requête SYNC (worker, threadpool) : les vecteurs dans l'ordre, et les tokens
    facturés (`usage.prompt_tokens`, ce que le fournisseur compte en entrée). Refuse un texte vide ou une requête au-delà des
    bornes (erreur de programmation, rien n'est envoyé). Lève `EchecEmbedding`."""
    if not texts:
        raise ValueError("embed_requete : aucun texte")
    if any(not t.strip() or _octets(t) > MAX_OCTETS_MORCEAU for t in texts) or \
            sum(_octets(t) for t in texts) > MAX_OCTETS_REQUETE:
        raise ValueError("embed_requete : texte vide ou au-delà des bornes d'octets")
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(_URL, headers=_headers(), json={"model": MODEL, "input": texts})
    except httpx.HTTPError as e:
        raise EchecEmbedding(f"réseau : {e}", definitif=False) from e
    if r.status_code >= 400:
        raise EchecEmbedding(f"HTTP {r.status_code} : {r.text[:500]}",
                             definitif=r.status_code in _DEFINITIFS)
    corps = r.json()
    data = sorted(corps["data"], key=lambda d: d["index"])
    if len(data) != len(texts):
        raise EchecEmbedding(f"{len(data)} vecteur(s) pour {len(texts)} texte(s)",
                             definitif=False)
    return [d["embedding"] for d in data], int(corps["usage"]["prompt_tokens"])


# Le client du chemin requête est PARTAGÉ : un client neuf par appel payait une poignée de
# main TLS à chaque recherche. Un par boucle d'événements, créé à la première requête : un
# `AsyncClient` lie son pool de connexions à la boucle qui l'a servi, et deux boucles (tests,
# processus annexes) ne doivent jamais se le passer. Le module ne crée rien à l'import —
# le registre est vide ; fermé à l'arrêt par le lifespan (`server.main`, `fermer_client`).
_DELAI_REQUETE_S = 8.0
_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary())


def _nouveau_client() -> httpx.AsyncClient:
    """Le client neuf — la couture où les tests montent un transport bouchon."""
    return httpx.AsyncClient(timeout=_DELAI_REQUETE_S)


def _client() -> httpx.AsyncClient:
    """Le client partagé de la boucle courante, créé au premier appel."""
    boucle = asyncio.get_running_loop()
    c = _CLIENTS.get(boucle)
    if c is None or c.is_closed:
        c = _CLIENTS[boucle] = _nouveau_client()
    return c


async def fermer_client() -> None:
    """Ferme le client partagé de la boucle courante (arrêt du serveur). Sans client, rien."""
    c = _CLIENTS.pop(asyncio.get_running_loop(), None)
    if c is not None:
        await c.aclose()


# Le jour (UTC) où CE processus a déjà dit que la recherche sémantique est coupée par le
# plafond : une ligne WARNING par jour et par processus, pas une par recherche.
_PLAFOND_DIT: set = set()


async def embed_query(text: str, *, timeout: float = _DELAI_REQUETE_S) -> Optional[list[float]]:
    """Un texte, ASYNC (chemin requête). None si désactivé, en échec ou au plafond
    journalier — la recherche retombe alors sur le lexical seul (jamais d'erreur remontée
    à l'agent), et le repli est journalisé (WARNING).

    Les tokens facturés comptent contre le MÊME plafond que l'indexation
    (`plafond_jour`, `embed_consommation`) : la base est lue et écrite au threadpool."""
    text = (text or "").strip()
    if not text or not enabled():
        return None
    from starlette.concurrency import run_in_threadpool
    from .db import embed_file
    t = _cap(text)
    try:
        jour = await run_in_threadpool(embed_file.jour)
        if jour["plafond_atteint"] or jour["tokens"] + borne_tokens([t]) > plafond_jour():
            cle = datetime.now(timezone.utc).date()
            if cle not in _PLAFOND_DIT:
                _PLAFOND_DIT.add(cle)
                logger.warning("embed_query : plafond journalier d'embeddings atteint "
                               "(%d tokens) — recherche en lexical seul jusqu'à minuit UTC",
                               jour["tokens"])
            return None
        r = await _client().post(_URL, headers=_headers(), timeout=timeout,
                                 json={"model": MODEL, "input": [t]})
        r.raise_for_status()
        corps = r.json()
        await run_in_threadpool(embed_file.ajouter_tokens, int(corps["usage"]["prompt_tokens"]))
        return corps["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001 — dégradation gracieuse vers le lexical
        logger.warning("embed_query échec (fallback lexical) : %s", e)
        return None


def to_pg(vec: list[float]) -> str:
    """Sérialise un vecteur au littéral pgvector/halfvec (`[a,b,c]`)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
