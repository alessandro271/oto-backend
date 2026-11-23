"""Worker d'indexation sémantique (lot 3) — draine les sources marquées à indexer.

Boucle de fond démarrée au boot (`boucles_de_fond`). Chaque tour, SOUS LE VERROU DE
BOUCLE (`verrou_de_boucle`, un seul processus à la fois sur la base), prend les pages,
puis les briefs et couches de contexte, puis les lignes de tableau opt-in, les RÉSERVE
une à une (`db.embed_file`, un bail en base) et les embarque hors event loop
(`run_in_threadpool` : l'appel réseau Mistral ne bloque jamais la boucle). No-op complet
sans `MISTRAL_API_KEY`.

Ce qui borne la dépense (novembre 2026 : 6,4 milliards de tokens en 23 jours, contre
59 millions en octobre) :

- **un morceau tient sous la limite du modèle par construction** : borne en octets
  (`embeddings.MAX_OCTETS_MORCEAU`), jamais une estimation en caractères ;
- **ce qui est payé est gardé** : chaque requête réussie est écrite aussitôt, morceau
  par morceau. Un échec plus loin dans la même page ne jette rien ;
- **un morceau inchangé ne se ré-embarque pas** : l'empreinte est PAR MORCEAU. Une
  retouche ne paie que les morceaux dont le texte a changé ; un morceau qui a seulement
  changé de rang reprend son vecteur ;
- **pas de retente infinie** : un refus du fournisseur (400, 413, 422) sur une requête
  à plusieurs morceaux renvoie chacun seul ; sur un morceau seul, il sort la source de
  la file, raison notée (`embed_sources.echec_raison`) — un état que le démarrage
  respecte (`db/_init.py` ne la remet pas à indexer). Une écriture ratée après un appel
  payé se compte ; au-delà de `_TENTATIVES_MAX` d'affilée sur le même texte, la source
  sort de la file de même. Un fournisseur indisponible (débit, serveur, réseau) arrête
  le tour sans rien compter (l'appel n'est pas facturé) et l'attente entre deux tours
  double, jusqu'à `_ATTENTE_MAX_S` ;
- **une source n'est indexée que stable** : inchangée depuis `OTO_EMBED_STABILITE_S`
  (défaut 10 min) — une rafale d'écritures se paie une fois, dans son état final ;
- **un plafond de tokens par jour et par instance** (`OTO_EMBED_PLAFOND_TOKENS_JOUR`) :
  une requête qui pourrait le dépasser n'est pas envoyée, le worker s'arrête pour la
  journée et l'équipe de la plateforme est prévenue, une fois par jour.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from starlette.concurrency import run_in_threadpool

from . import db, embeddings
from .db import embed_file, verrou_de_boucle

logger = logging.getLogger(__name__)

NOM = "embed_worker"
_POLL_S = 20
_BATCH = 16
# Bail d'une source réservée, renouvelé après chaque requête payée : une requête dure au
# plus son délai (30 s) ; un processus tué rend la source à l'échéance.
_BAIL_S = 600
# Écritures ratées en base après un appel payé, d'affilée sur le même texte : au-delà,
# la source sort de la file (chaque tentative repaie les morceaux non écrits).
_TENTATIVES_MAX = 5
# Fournisseur indisponible (débit, erreur serveur, réseau) : l'attente entre deux tours
# double à chaque tour arrêté ainsi, jusqu'à ce plafond, et revient à `_POLL_S` au
# premier tour qui passe. Un appel refusé ainsi n'est pas facturé : il ne compte pas
# contre la source.
_ATTENTE_MAX_S = 1800
# Délai de stabilité par défaut : une source n'est indexée que si elle n'a pas été
# écrite depuis ce délai. Une page retravaillée (149 révisions en trois semaines pour
# l'une d'elles) s'écrit par rafales de quelques minutes ; 10 minutes laissent passer
# la rafale et paient l'état final, pour un retard de la recherche sémantique que la
# recherche plein texte, immédiate, couvre.
STABILITE_DEFAUT_S = 600

def stabilite_s() -> int:
    """`OTO_EMBED_STABILITE_S` : secondes sans écriture avant qu'une source soit indexée.
    Absent ou vide : `STABILITE_DEFAUT_S`. `0` : aussitôt. Illisible : LÈVE."""
    brut = os.environ.get("OTO_EMBED_STABILITE_S")
    if brut is None or brut.strip() == "":
        return STABILITE_DEFAUT_S
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = -1
    if valeur < 0:
        raise ValueError(f"OTO_EMBED_STABILITE_S={brut!r} illisible : un entier positif "
                         "(secondes), `0` indexe aussitôt.")
    return valeur


def prochaine_attente(attente: int, bilan: "Optional[Bilan]") -> int:
    """L'attente avant le tour suivant : double après un tour arrêté par le fournisseur,
    jusqu'à `_ATTENTE_MAX_S` ; `_POLL_S` sinon."""
    if bilan is not None and bilan.fournisseur_indisponible:
        return min(max(attente, _POLL_S) * 2, _ATTENTE_MAX_S)
    return _POLL_S


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


# ── les sources ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    """Une sorte de source indexée, et comment le worker la lit et l'écrit.

    `stockes(ref)` : `{rang: {sha, embedding?}}` des vecteurs posés ; `poser(ref, rang,
    sha, littéral)` en écrit un ; `finir(ref, n, sha_texte)` clôt une source indexée en
    `n` morceaux ; `sortir(ref)` baisse son drapeau sans l'indexer (échec)."""
    nom: str
    lister: Callable[[int, int], list[dict]]     # (n, stabilité s) → [{ref, text}]
    multi: bool                                  # plusieurs morceaux, ou le premier seul
    stockes: Callable[[str], dict]
    poser: Callable[[str, int, str, str], None]
    finir: Callable[[str, int, str], None]
    sortir: Callable[[str], None]


def _docs() -> Source:
    return Source(
        nom="doc",
        lister=lambda n, st: [{"ref": str(r["id"]), "text": r["text"]}
                              for r in db.list_dirty_docs(n, stable_s=st)],
        multi=True,
        stockes=lambda ref: db.doc_morceaux_stockes(int(ref)),
        poser=lambda ref, rang, sha, lit: db.poser_morceau_doc(
            int(ref), rang, sha, lit, embeddings.MODEL),
        finir=lambda ref, n, sha: db.finir_doc(int(ref), n, sha),
        sortir=lambda ref: db.clear_embed_dirty(int(ref)),
    )


def _aux(kind: str) -> Source:
    def stockes(ref: str) -> dict:
        sha = db.get_aux_embedding_sha(kind, int(ref))
        return {0: {"sha": sha}} if sha else {}

    return Source(
        nom=kind,
        lister=lambda n, st: [{"ref": str(r["ref"]), "text": r["text"]}
                              for r in db.list_dirty_aux(n, stable_s=st) if r["kind"] == kind],
        multi=False,
        stockes=stockes,
        poser=lambda ref, rang, sha, lit: db.upsert_aux_embedding(
            kind, int(ref), sha, lit, embeddings.MODEL),
        finir=lambda ref, n, sha: db.clear_aux_dirty(kind, int(ref)),
        sortir=lambda ref: db.clear_aux_dirty(kind, int(ref)),
    )


def _ligne(ref: str) -> tuple[int, str]:
    ns, row = ref.split("/", 1)
    return int(ns), row


def _lignes() -> Source:
    def stockes(ref: str) -> dict:
        sha = db.get_row_embedding_sha(*_ligne(ref))
        return {0: {"sha": sha}} if sha else {}

    return Source(
        nom="row",
        lister=lambda n, st: [{"ref": f"{r['ns_id']}/{r['row_id']}", "text": r["text"]}
                              for r in db.list_dirty_rows(n, stable_s=st)],
        multi=False,
        stockes=stockes,
        poser=lambda ref, rang, sha, lit: db.upsert_row_embedding(
            *_ligne(ref), sha, lit, embeddings.MODEL),
        finir=lambda ref, n, sha: db.clear_row_dirty(*_ligne(ref)),
        sortir=lambda ref: db.clear_row_dirty(*_ligne(ref)),
    )


def sources() -> tuple[Source, ...]:
    return (_docs(), _aux("brief"), _aux(db.NODE_KIND), _lignes())


# ── le tour ──────────────────────────────────────────────────────────────────

class _Arret(Exception):
    """Le tour s'arrête : plafond du jour atteint, fournisseur indisponible, écriture
    ratée."""

    def __init__(self, raison: str, *, fournisseur: bool = False):
        super().__init__(raison)
        self.fournisseur = fournisseur


@dataclass
class Bilan:
    indexees: int = 0
    inchangees: int = 0
    en_echec: int = 0
    tokens: int = 0
    arret: Optional[str] = None
    fournisseur_indisponible: bool = False


def _plan(src: Source, ref: str, text: str) -> tuple[list, list]:
    """Ce qu'une source demande : `(morceaux, a_payer)`, où `morceaux` = [(rang, texte,
    sha)] — vide si les vecteurs posés valent pour ce texte — et `a_payer` les seuls
    morceaux dont aucun vecteur posé ne porte l'empreinte. Un morceau dont l'empreinte
    est posée à un AUTRE rang est recopié ici même, sans requête."""
    stockes = src.stockes(ref)
    # Vecteurs posés avant l'empreinte par morceau : `content_sha` du rang 0 y est celle
    # du texte entier. Texte inchangé → ils valent, rien à faire.
    if stockes.get(0, {}).get("sha") == _sha(text):
        return [], []
    textes = embeddings.chunks(text)
    if not src.multi:
        textes = textes[:1]
    morceaux = [(i, t, _sha(t)) for i, t in enumerate(textes)]
    par_sha = {v["sha"]: v.get("embedding") for v in stockes.values()}
    a_payer = []
    for i, t, sha in morceaux:
        if stockes.get(i, {}).get("sha") == sha:
            continue
        lit = par_sha.get(sha)
        if lit is not None:
            src.poser(ref, i, sha, lit)       # le même texte, à un autre rang
        else:
            a_payer.append((i, t, sha))
    return morceaux, a_payer


def _arreter_au_plafond(consommes: int, plafond: int) -> None:
    if embed_file.marquer_plafond():
        logger.error("embed_worker : plafond journalier atteint (%d tokens consommés, "
                     "plafond %d) — indexation arrêtée jusqu'à minuit UTC", consommes, plafond)
        _alerter_equipe(consommes, plafond)
    raise _Arret("plafond journalier atteint")


def _indexer(src: Source, ref: str, text: str, bilan: Bilan, plafond: int) -> None:
    texte_sha = _sha(text)
    etat = embed_file.echec(src.nom, ref)
    if etat and etat["echec_raison"] and etat["sha"] == texte_sha:
        # Déjà sortie en échec sur ce texte-là, et remarquée sans qu'il change : rien
        # ne se renvoie.
        src.sortir(ref)
        bilan.en_echec += 1
        return
    morceaux, a_payer = _plan(src, ref, text)
    if not morceaux:
        # Texte inchangé depuis l'ancienne empreinte : les vecteurs restent tels quels.
        # Texte vide : `finir(…, 0, …)` retire les vecteurs d'une page vidée.
        if text.strip():
            src.sortir(ref)
        else:
            src.finir(ref, 0, texte_sha)
        embed_file.reussir(src.nom, ref)
        bilan.inchangees += 1
        return
    if not a_payer:
        src.finir(ref, len(morceaux), texte_sha)
        embed_file.reussir(src.nom, ref)
        bilan.inchangees += 1
        return
    file = _decouper(a_payer)
    while file:
        lot = file.pop(0)
        textes = [t for _, t, _ in lot]
        jour = embed_file.jour()
        if jour["plafond_atteint"] or jour["tokens"] + embeddings.borne_tokens(textes) > plafond:
            _arreter_au_plafond(jour["tokens"], plafond)
        try:
            vecteurs, tokens = embeddings.embed_requete(textes)
        except embeddings.EchecEmbedding as e:
            if not e.definitif:
                logger.warning("embed_worker : fournisseur indisponible (%s %s) : %s",
                               src.nom, ref, e.raison)
                raise _Arret(e.raison, fournisseur=True) from e
            if len(lot) > 1:
                # Un refus sur une requête à plusieurs morceaux : chacun repart seul,
                # pour ne sortir que la source fautive sans perdre les autres.
                file[:0] = [[m] for m in lot]
                continue
            # La réponse du fournisseur (tronquée), jamais le texte envoyé : c'est le
            # contenu d'un tenant. Le compte de tokens refusé n'est pas rendu par un 400 :
            # on donne son majorant (un token par octet).
            rang = lot[0][0]
            logger.warning("embed_worker : %s %s, morceau %d refusé (≤ %d tokens) : %s",
                           src.nom, ref, rang, embeddings.borne_tokens(textes), e.raison)
            _sortir_en_echec(src, ref, texte_sha, e.raison, bilan)
            return
        bilan.tokens += tokens
        embed_file.ajouter_tokens(tokens)
        for (i, _t, sha), vec in zip(lot, vecteurs):
            src.poser(ref, i, sha, embeddings.to_pg(vec))
        embed_file.prolonger(src.nom, ref, _BAIL_S)
    src.finir(ref, len(morceaux), texte_sha)
    embed_file.reussir(src.nom, ref)
    bilan.indexees += 1


def _decouper(a_payer: list) -> list[list]:
    """Les requêtes d'une source : ses morceaux à payer, groupés sous les bornes."""
    out, i = [], 0
    for lot in embeddings.lots([t for _, t, _ in a_payer]):
        out.append(a_payer[i:i + len(lot)])
        i += len(lot)
    return out


def _passager(src: Source, ref: str, texte_sha: str, raison: str, bilan: Bilan) -> None:
    """Une écriture (ou lecture) en base ratée après un appel payé : ce qui n'a pas été
    écrit se repaiera. Compté sur ce texte : en deçà de `_TENTATIVES_MAX`, le
    tour s'arrête et la source reste dans la file ; au-delà, elle en sort en échec.
    Lève toujours `_Arret`."""
    n = embed_file.noter_passager(src.nom, ref, texte_sha)
    if n < _TENTATIVES_MAX:
        logger.warning("embed_worker : %s %s, indexation interrompue %d/%d : %s",
                       src.nom, ref, n, _TENTATIVES_MAX, raison)
    else:
        _sortir_en_echec(src, ref, texte_sha, raison, bilan)
    raise _Arret(raison)


def _sortir_en_echec(src: Source, ref: str, texte_sha: str, raison: str,
                     bilan: Bilan) -> None:
    logger.error("embed_worker : %s %s sort de la file d'indexation en échec : %s",
                 src.nom, ref, raison)
    embed_file.noter_echec(src.nom, ref, texte_sha, raison)
    src.sortir(ref)
    bilan.en_echec += 1


def _tour() -> Bilan:
    """Un tour SYNC (threadpool, sous le verrou de boucle)."""
    bilan = Bilan()
    plafond = embeddings.plafond_jour()
    stable = stabilite_s()
    if embed_file.jour()["plafond_atteint"]:
        bilan.arret = "plafond journalier atteint"
        return bilan
    try:
        for src in sources():
            rows = src.lister(_BATCH, stable)
            tenues = embed_file.reserver(src.nom, [r["ref"] for r in rows], _BAIL_S)
            for r in rows:
                if r["ref"] not in tenues:
                    continue
                try:
                    _indexer(src, r["ref"], r["text"], bilan, plafond)
                except _Arret:
                    raise
                except Exception as e:  # noqa: BLE001 — écriture ou lecture en base ratée
                    # Ce qui a été payé et écrit reste ; ce qui ne l'a pas été se
                    # repaiera au tour suivant : la tentative est comptée, bornée.
                    logger.exception("embed_worker : %s %s, indexation interrompue",
                                     src.nom, r["ref"])
                    _passager(src, r["ref"], _sha(r["text"]),
                              f"{type(e).__name__} : {e}", bilan)
                finally:
                    embed_file.relacher(src.nom, r["ref"])
    except _Arret as e:
        bilan.arret = str(e)
        bilan.fournisseur_indisponible = e.fournisseur
    return bilan


# ── l'alerte ─────────────────────────────────────────────────────────────────

def _alerter_equipe(consommes: int, plafond: int) -> None:
    """Le courriel aux admins de plateforme de l'instance (`alertes_equipe`). Personne,
    ou un envoi refusé : une ligne ERROR, l'arrêt du worker tient de toute façon."""
    from . import alertes_equipe, email_templates
    equipe = alertes_equipe.destinataires()
    if not equipe:
        logger.error("embed_worker : aucun admin de plateforme actif sur l'instance — "
                     "alerte de plafond d'indexation NON envoyée")
        return
    for adresse in equipe:
        if not email_templates.send_embed_plafond_email(
                adresse, consommes=consommes, plafond=plafond):
            logger.error("embed_worker : alerte de plafond refusée pour %s",
                         html.escape(adresse))


async def run_embed_loop(interval: int = _POLL_S) -> None:
    if not embeddings.enabled():
        logger.info("embed_worker: MISTRAL_API_KEY absent → sémantique inerte, worker off.")
        return
    embeddings.plafond_jour()   # un réglage illisible se dit au démarrage de la boucle
    stabilite_s()
    logger.info("embed_worker: démarré (poll %ss).", interval)
    attente = interval
    while True:
        b = None
        try:
            b = await run_in_threadpool(verrou_de_boucle.sous_verrou, NOM, _tour,
                                        si_saute=None)
            if b is not None and (b.indexees or b.en_echec or b.arret or b.tokens):
                logger.info("embed_worker: %d source(s) indexée(s), %d inchangée(s), "
                            "%d en échec, %d tokens%s.", b.indexees, b.inchangees,
                            b.en_echec, b.tokens, f" — arrêt : {b.arret}" if b.arret else "")
        except Exception as e:  # noqa: BLE001
            logger.warning("embed_worker: tour en échec : %s", e)
        attente = prochaine_attente(attente, b) if b is not None else interval
        await asyncio.sleep(attente)
