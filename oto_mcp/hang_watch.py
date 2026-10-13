"""Détecte un BLOCAGE de l'event loop et journalise la VRAIE pile qui bloque —
complète `loop_watch.py`, qui journalise APRÈS coup et sans dire OÙ.

`loop_watch.enable()` (aiodebug.log_slow_callbacks) chronomètre un callback et
n'appelle `on_slow_callback` qu'APRÈS son exécution : au moment où il tourne, la pile
qui bloquait s'est déjà déroulée — un `sys._current_frames()` là ne montrerait que la
sonde elle-même, jamais le coupable. `aiodebug.hang_inspection` (même lib, jamais
branché ici) résout le problème par un THREAD SÉPARÉ qui surveille un timestamp
partagé, mis à jour par une tâche minuscule de la boucle : si le délai dépasse le
seuil, la boucle est bloquée EN CE MOMENT, et c'est ce thread — qui tourne PENDANT le
blocage, pas après — qui peut dumper la vraie pile.

Adapté ici, sur le même patron mais pas la même sortie :
- **journalise** (logger `oto_mcp.loop`, comme `loop_watch.py`) plutôt qu'écrire des
  fichiers `stacktrace-*.txt` sur la box ;
- **UNE entrée de journal par gel** (#1108, 12/10) : la pile de **tous les fils** du
  process (`sys._current_frames()` × `threading.enumerate()`), chacun avec son nom,
  son `ident`, son `tid` noyau, s'il est démon, ses `_CADRES_PAR_FIL` derniers cadres
  (`chemin:ligne fonction`, du plus profond au plus haut), le fil de la BOUCLE marqué
  et en tête, et ce que le relevé sait dire sans dépendance : le fil est-il dans un
  appel C (instruction courante de son cadre le plus profond) ou entre deux
  instructions Python, son état et son CPU cumulé (`/proc/self/task/<tid>/stat`). Les
  fils au repos (selector, file d'attente, `wait`) sont regroupés par pile identique.
  Chaque ligne de l'entrée porte `gel=<hex>` : le journal de la box (journald) range
  chaque ligne de stderr à part, un `grep gel=<hex>` rend l'entrée entière. Plafonnée à
  `_PLAFOND_RELEVE` caractères, la troncature dite. Pourquoi tous : une boucle trouvée
  dans `select` n'est pas bloquée par son propre code, elle attend le GIL qu'un AUTRE
  fil tient (`docs/event-loop-perf.md`, gels « select ») ;
- le **retard du chien lui-même** et le CPU du process depuis son réveil précédent :
  le chien a besoin du GIL pour vérifier, un chien en retard d'autant que le gel dit
  que le GIL (ou le process entier) lui a manqué aussi ;
- **aucune variable locale** (rien que `f_code`, `f_lineno` et `f_lasti` de chaque
  cadre, jamais `f_locals`, `format_exc` ni texte source) — même famille de risque que le fix
  Sentry du 2026-09-15 (#564, `include_local_variables=False`) : un secret déchiffré
  qui traînerait dans une frame ne doit jamais atteindre un journal ;
- **débit limité** (nuit du 14-15/09 : une salve de 6 blocages courts en ~1 min suite
  à une bascule) : UN dump par ÉPISODE de blocage — un état armé tant que le délai
  reste dépassé, réarmé proprement à la résorption — plus un plafond par minute toutes
  causes confondues, pour qu'une salve ne remplisse pas le journal de dumps redondants
  du même incident sans rien ajouter au premier ;
- **coupé au process, pas au déploiement** : `OTO_HANG_WATCH_ENABLED` (défaut actif)
  coupe le mécanisme par un redémarrage, sans redéployer. Le mécanisme est la réponse
  directe à des gels de production dont la cause reste non identifiée
  (`docs/event-loop-perf.md`) — le désactiver par défaut reviendrait à se priver de la
  preuve le jour où elle sert. Actif par défaut est défendable seulement parce que le
  coût, lui, est mesuré (pas supposé) : `tests/test_hang_watch.py` chiffre le coût de
  la tâche qui bat le timestamp (~0,1 µs/appel — négligeable). Le coût du thread
  watchdog lui-même SUIT LA FRÉQUENCE DE RÉVEIL — pas du bruit de machine, un coût
  réel et attendu du mécanisme à haute fréquence : mesuré dans le test à un réveil
  de 10 ms (réveil toutes les 5 ms, `interval/2`) : **12,7 % de CPU pour le thread
  sur 10s, et le débit de la boucle perd 9 %**. Au seuil réel de prod
  (`OTO_SLOW_CALLBACK_WARN=1.0s`, réveil toutes les 500 ms — 100× moins fréquent),
  isolé (`/proc/self/task/<tid>/stat`, fenêtre de 180s, 360 réveils, demande de la
  session de déploiement le 15/09/2026) : **0,020s CPU sur 180s, soit 0,011 % d'un
  cœur**. Sans commune mesure avec le coût d'un aller-retour réseau ou DB qu'un vrai
  handler paierait de toute façon. Le nom suit le patron déjà en place pour les
  boucles de fond
  (`OTO_SCHEDULER_ENABLED`, `OTO_BILLING_RUNNER_ENABLED`… — `boucles_de_fond.py`),
  pas celui de `OTO_SLOW_CALLBACK_WARN`/`_SENTRY` (des seuils, pas des interrupteurs).

Le seuil de déclenchement reste `OTO_SLOW_CALLBACK_WARN` (déf. 1.0s) — même sonde,
même vocabulaire de "lent", pas un second seuil à régler séparément.

⚠️ La sonde ne casse JAMAIS la boucle qu'elle observe (même principe que
`loop_watch.py`) : toute écriture de log est enveloppée dans un `try/except` qui ne
peut jamais lever, et la tâche côté boucle qui bat le timestamp ne fait rien d'autre
qu'écrire un flottant avant de redormir.
"""
from __future__ import annotations

import asyncio
import dis
import logging
import os
import sys
import threading
import time
from types import FrameType
from typing import NamedTuple

logger = logging.getLogger("oto_mcp.loop")

OTO_HANG_WATCH_ENABLED = "OTO_HANG_WATCH_ENABLED"

# Plafond de dumps par minute, toutes causes confondues. Pas exposé par variable
# d'env (contrairement à `OTO_HANG_WATCH_ENABLED`) : ce n'est pas un besoin exprimé,
# et chaque interrupteur de plus est une surface de plus à documenter et à retenir.
# 3 : assez pour voir si une salve porte plusieurs causes distinctes (les dumps
# suivants ne répètent que le comptage, jamais le texte), pas assez pour qu'une
# salve de blocages courts et rapprochés (6 en ~1 min, vécu la nuit du 14-15/09)
# remplisse le journal de piles redondantes du même incident.
_MAX_DUMPS_PER_MIN = 3

# Cadres gardés par fil : les plus profonds, ceux qui disent ce que le fil fait. 20
# couvrent le chemin d'une requête depuis le cadre qui bloque jusqu'au handler (la
# racine uvicorn/starlette, au-delà, ne dit rien d'un gel). Un fil au repos n'en garde
# que `_CADRES_AU_REPOS` : son point d'attente et qui l'y a mis.
_CADRES_PAR_FIL = 20
_CADRES_AU_REPOS = 3

# Taille maximale d'un relevé (caractères, toutes lignes comprises). Un relevé part
# dans le journal ET, par la LoggingIntegration, en fil d'Ariane du prochain événement
# Sentry : sans plafond, un process à cent fils actifs écrirait plus de 100 Ko par gel
# dans un journal qui ne garde déjà que quelques heures. 32 000 : la boucle, une
# vingtaine de fils actifs à 20 cadres et un pool de quarante fils au repos (mesuré :
# 61 fils, ~20 Ko) ; au-delà, la troncature se DIT, avec le nombre de fils non
# montrés, et les fils au repos partent les premiers.
_PLAFOND_RELEVE = 32_000
# Réservés sous le plafond pour la ligne finale (« fin du relevé » ou « tronqué ») et
# l'éventuel avertissement « fil de la boucle absent ».
_RESERVE_FIN = 300

# (fichier, fonction) du cadre le plus profond d'un fil qui ATTEND sans rien tenir :
# regroupé avec ses semblables, sa pile ne dit rien du gel. `threading.wait` couvre
# `Condition.wait`, `Event.wait` et `queue.Queue.get` (pool anyio, pool psycopg,
# transport Sentry) ; `thread.py:_worker` est l'attente d'un pool
# `concurrent.futures` sur sa `SimpleQueue` (C, donc sans cadre Python à elle).
_ATTENTES = frozenset({
    ("selectors.py", "select"),
    ("threading.py", "wait"),
    ("threading.py", "_wait_for_tstate_lock"),
    ("queue.py", "get"),
    ("thread.py", "_worker"),
})

# Instructions qui appellent du code (C quand aucun cadre Python n'est au-dessus) :
# `CALL*` (3.10 à 3.14), `PRECALL` (3.11), l'entrée d'un `with` (3.11-3.13 ; un CALL
# en 3.14). Le cadre le plus profond d'un fil arrêté sur l'une d'elles est DANS un appel
# natif : `time.sleep`, `lock.acquire`, `select`, `json.dumps`, `re.match`…
_APPELS = frozenset(
    nom for nom in dis.opname
    if nom.startswith("CALL") or nom in {"PRECALL", "BEFORE_WITH", "BEFORE_ASYNC_WITH"}
)


def enabled() -> bool:
    """Lu au démarrage (redémarrage du process pour changer d'avis, pas de redéploi).
    Absent ou toute valeur ≠ "0" ⟹ actif — fail-open vers le comportement utile, jamais
    une erreur de boot pour une variable mal orthographiée. Même convention que les
    interrupteurs de boucles de fond (`boucles_de_fond._interrupteur`)."""
    return os.environ.get(OTO_HANG_WATCH_ENABLED, "1") != "0"


class HangWatch(threading.Thread):
    """Thread démon : observe un timestamp partagé (mis à jour par `beat()`, appelé
    depuis la boucle) et journalise — au plus une fois par ÉPISODE de blocage, borné
    en plus par minute — UN relevé de tous les fils (`releve_des_fils`), pris pendant
    que la boucle bloque réellement, sous un identifiant `gel=<hex>`.

    `_last_beat` est une liste à un élément (pas un flottant nu) : Python la passe
    par RÉFÉRENCE, donc `beat()` (thread de la boucle) et `_check()` (ce thread) voient
    la même case — même patron qu'`aiodebug.hang_inspection`. Aucun verrou : sous le
    GIL, une écriture/lecture d'un flottant dans une liste ne se déchire pas."""

    def __init__(self, interval: float, max_dumps_per_min: int = _MAX_DUMPS_PER_MIN,
                 plafond: int = _PLAFOND_RELEVE) -> None:
        super().__init__(daemon=True, name="oto-hang-watch")
        self._interval = interval
        self._max_dumps_per_min = max_dumps_per_min
        self._plafond = plafond
        self._last_beat = [time.monotonic()]
        self._stop_event = threading.Event()
        self._episode_open = False
        self._episode_started_at = 0.0
        self._episode_id = ""
        self._dump_times: list[float] = []
        self._main_thread_id = threading.main_thread().ident
        # Réveil précédent du chien : horloge murale et CPU du process (tous fils).
        # Relus à chaque réveil, pas seulement au dump — c'est leur écart qui dit si
        # le chien a lui-même attendu le GIL pendant le gel.
        self._prev_check = time.monotonic()
        self._prev_cpu = time.process_time()

    def beat(self) -> None:
        """Appelé depuis la boucle asyncio — coût : une écriture de flottant."""
        self._last_beat[0] = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # thread démon, jamais join()
        # `Event.wait` relâche le GIL pendant l'attente (contrairement à `time.sleep`
        # sous charge du GIL, mais les deux le font) : le coût de ce thread AU REPOS
        # est le réveil périodique, pas l'attente elle-même.
        while not self._stop_event.wait(self._interval / 2):
            self._check()

    def _check(self) -> None:
        now = time.monotonic()
        cpu = time.process_time()
        depuis_reveil, self._prev_check = now - self._prev_check, now
        cpu_depuis_reveil, self._prev_cpu = cpu - self._prev_cpu, cpu
        # Le chien devait se réveiller `interval/2` après le réveil précédent : ce qui
        # dépasse, il l'a passé à attendre le GIL (ou le process entier était arrêté).
        retard = max(0.0, depuis_reveil - self._interval / 2)
        fenetre = (retard, cpu_depuis_reveil, depuis_reveil)
        delay = now - self._last_beat[0]
        if delay > self._interval:
            if not self._episode_open:
                self._episode_open = True
                self._episode_started_at = now
                self._episode_id = os.urandom(3).hex()
                self._maybe_dump(delay, fenetre, boucle_repartie=False)
            # sinon : même épisode, déjà relevé — on attend la résorption
            return
        if self._episode_open:
            self._episode_open = False
            duration = now - self._episode_started_at
            self._safe_log(logging.WARNING, "event loop débloquée après %.1fs (gel=%s)",
                           duration, self._episode_id)
        elif retard > self._interval:
            # Gel VU PAR LE CHIEN SEUL : ni lui ni la boucle n'ont eu le GIL pendant
            # plus que le seuil, mais à sa libération la boucle l'a repris la première
            # et a battu — le délai de battement ne dit plus rien. Sans cette branche,
            # un fil qui tient le GIL échappe au chien une fois sur deux ou plus
            # (`docs/event-loop-perf.md`, relevé « tous les fils »).
            self._episode_id = os.urandom(3).hex()
            self._maybe_dump(retard, fenetre, boucle_repartie=True)

    def _maybe_dump(self, delay: float, fenetre: tuple[float, float, float], *,
                    boucle_repartie: bool) -> None:
        """`delay` est mesuré depuis le dernier `beat()` — qui a lieu toutes les
        `interval/2` — pas depuis le début réel d'un éventuel blocage : le vrai
        blocage a pu commencer jusqu'à `interval/2` APRÈS le `delay` rapporté ici.
        D'où le libellé « sans battement depuis » plutôt que « bloquée » : ce
        nombre peut SURESTIMER le temps de blocage réel, jusqu'à `interval/2`
        (0,5s au seuil de prod) — ce n'est pas une mesure exacte du blocage, juste
        de l'absence de battement. La durée totale du gel, elle, part à la
        résorption (« débloquée après »).

        `boucle_repartie` : le gel n'est vu que par le retard du chien, la boucle
        a déjà repris — son cadre ne montre que son travail ordinaire."""
        constat = ("event loop et chien à l'arrêt pendant %.1fs (GIL tenu ailleurs ou "
                   "process suspendu), la boucle a repris la première" if boucle_repartie
                   else "event loop sans battement depuis %.1fs")
        now = time.monotonic()
        self._dump_times = [t for t in self._dump_times if now - t < 60.0]
        if len(self._dump_times) >= self._max_dumps_per_min:
            self._safe_log(
                logging.WARNING,
                constat + " (gel=%s) — dump ignoré (plafond de %d/min atteint, "
                "probablement la même salve)",
                delay, self._episode_id, self._max_dumps_per_min,
            )
            return
        self._dump_times.append(now)
        # Un retard du chien du même ordre que le gel = il a attendu le GIL (ou le
        # process entier était arrêté) ; le CPU du process sur la même fenêtre
        # départage les deux (quelqu'un calculait, ou personne).
        entete = (constat % delay + " (gel=%s) — chien en retard de %.2fs, CPU du process "
                  "%.2fs sur les %.2fs depuis son réveil précédent") % (
                      self._episode_id, *fenetre)
        try:
            texte = releve_des_fils(self._episode_id, entete, boucle=self._main_thread_id,
                                    plafond=self._plafond)
        except Exception:
            # Le relevé ne doit jamais tuer le chien : le constat part quand même.
            logger.warning("gel=%s : relevé des fils impossible", self._episode_id,
                           exc_info=True)
            texte = entete + " — relevé des fils impossible, cf. l'avertissement précédent"
        self._safe_log(logging.WARNING, "%s", texte)

    @staticmethod
    def _safe_log(level: int, msg: str, *args: object) -> None:
        try:
            logger.log(level, msg, *args)
        # noqa: SILENT — la sonde de boucle ne casse jamais la boucle qu'elle observe
        except Exception:
            pass


class _Fil(NamedTuple):
    nom: str
    ident: int
    tid: int | None       # identifiant noyau (`Thread.native_id`), celui de py-spy et de /proc
    demon: bool | None    # None : fil inconnu de `threading` (créé hors du module)
    cadres: tuple[tuple[str, int, str], ...]
    natif: bool           # cadre le plus profond arrêté sur un appel : DANS du code C
    attente: str | None   # « threading.wait »… si le fil est au repos


def releve_des_fils(gel: str, entete: str, *, boucle: int | None,
                    plafond: int = _PLAFOND_RELEVE) -> str:
    """Le relevé d'un gel : UNE entrée multi-lignes, `entete` en tête, puis un bloc par
    fil (ou par groupe de fils au repos à pile identique), chaque ligne après la
    première préfixée de `gel=<gel>`. Le fil appelant (le chien) est exclu.

    Rien que `f_code`, `f_lineno` et `f_lasti` : ni variable locale, ni texte source
    (pas de `linecache`, donc aucune lecture disque tant que le chien tient le GIL).
    Le CPU et l'état de chaque fil se lisent dans `/proc` (Linux ; ailleurs, omis).

    Ordre : la boucle ; les fils actifs entre deux instructions Python (ils
    calculaient en Python et attendent le GIL) ; les fils actifs dans un appel C ;
    les groupes au repos. Au plafond, les derniers blocs tombent et la dernière ligne
    le dit."""
    # L'instantané d'abord, au plus près du moment où le chien a repris le GIL.
    frames = sys._current_frames()
    connus = {t.ident: t for t in threading.enumerate()}
    moi = threading.get_ident()
    fil_boucle: _Fil | None = None
    actifs: list[_Fil] = []
    repos: list[_Fil] = []
    for ident, frame in frames.items():
        if ident == moi:
            continue
        t = connus.get(ident)
        cadres = _cadres(frame, _CADRES_PAR_FIL)
        fichier, _, fonction = cadres[0] if cadres else ("?", 0, "?")
        point = (os.path.basename(fichier), fonction)
        attente = (f"{point[0].removesuffix('.py')}.{fonction}"
                   if ident != boucle and point in _ATTENTES else None)
        fil = _Fil(
            nom=t.name if t is not None else f"tid-{ident}",
            ident=ident,
            tid=getattr(t, "native_id", None) if t is not None else None,
            demon=t.daemon if t is not None else None,
            cadres=cadres[:_CADRES_AU_REPOS] if attente else cadres,
            natif=_dans_un_appel(frame),
            attente=attente,
        )
        if ident == boucle:
            fil_boucle = fil
        elif attente:
            repos.append(fil)
        else:
            actifs.append(fil)

    blocs: list[tuple[list[_Fil], str]] = []
    if fil_boucle is not None:
        blocs.append(([fil_boucle], "BOUCLE"))
    actifs.sort(key=lambda f: f.natif)  # stable : les fils « en Python » d'abord
    blocs.extend(([f], "") for f in actifs)
    groupes: dict[tuple, list[_Fil]] = {}
    for f in repos:
        groupes.setdefault((f.attente, f.cadres, f.natif), []).append(f)
    blocs.extend((membres, f"au repos ({membres[0].attente})") for membres in groupes.values())

    n_fils = sum(len(m) for m, _ in blocs)
    prefixe = f"gel={gel} "
    lignes = [f"{entete} — {n_fils} fils relevés, cadres du plus profond au plus haut"]
    taille = len(lignes[0])
    montres = 0
    for membres, marque in blocs:
        bloc = _bloc(prefixe, membres, marque)
        poids = sum(len(l) + 1 for l in bloc)
        if taille + poids > plafond - _RESERVE_FIN:
            break
        lignes.extend(bloc)
        taille += poids
        montres += len(membres)
    if montres < n_fils:
        lignes.append(f"{prefixe}… relevé tronqué au plafond de {plafond} caractères : "
                      f"{n_fils - montres} fil(s) non montré(s)")
    else:
        lignes.append(f"{prefixe}fin du relevé : {n_fils} fils")
    if boucle is not None and fil_boucle is None:
        lignes.insert(1, f"{prefixe}⚠ fil de la boucle (ident={boucle}) absent de "
                         "sys._current_frames()")
    return "\n".join(lignes)


def _bloc(prefixe: str, membres: list[_Fil], marque: str) -> list[str]:
    """`» <fil> [BOUCLE] <appel C|en Python> …` puis un cadre par ligne. Un groupe au
    repos : `» ×N au repos (…) : <fil> ; <fil> …`, ses cadres une seule fois."""
    tete = membres[0]
    nature = "appel C" if tete.natif else "en Python"
    if len(membres) == 1:
        entete = f"{prefixe}» {_identite(tete)} " + " ".join(
            x for x in (marque, nature) if x)
    else:
        entete = (f"{prefixe}» ×{len(membres)} {marque}, {nature} : "
                  + " ; ".join(_identite(f) for f in membres))
    return [entete] + [f"{prefixe}    {_chemin_court(fichier)}:{ligne} {fonction}"
                       for fichier, ligne, fonction in tete.cadres]


def _identite(fil: _Fil) -> str:
    demon = {True: "oui", False: "non", None: "?"}[fil.demon]
    texte = f"« {fil.nom} » ident={fil.ident} tid={fil.tid if fil.tid else '?'} démon={demon}"
    etat_cpu = _etat_et_cpu(fil.tid)
    if etat_cpu is not None:
        texte += f" état={etat_cpu[0]} cpu={etat_cpu[1]:.2f}s"
    return texte


def _cadres(frame: FrameType | None, n: int) -> tuple[tuple[str, int, str], ...]:
    """Les `n` cadres les plus profonds, le plus profond d'abord."""
    cadres: list[tuple[str, int, str]] = []
    while frame is not None and len(cadres) < n:
        cadres.append((frame.f_code.co_filename, frame.f_lineno or 0, frame.f_code.co_name))
        frame = frame.f_back
    return tuple(cadres)


def _dans_un_appel(frame: FrameType) -> bool:
    """Le fil est-il DANS du code natif ? Son cadre Python le plus profond est arrêté
    sur une instruction d'appel (`_APPELS`) : l'appelé n'a pas de cadre Python, c'est
    du C — un appel bloquant qui a rendu le GIL (`sleep`, `acquire`, `select`, E/S)
    ou un calcul C qui vient de le rendre. Sinon, le fil s'est arrêté ENTRE deux
    instructions Python : il calculait en Python et a cédé le GIL à la bascule.

    `f_lasti` est en octets depuis 3.10 et `co_code` rend le code non spécialisé : une
    lecture d'octet, sans désassembler."""
    lasti = frame.f_lasti
    code = frame.f_code.co_code
    if lasti < 0 or lasti >= len(code):
        return False
    return dis.opname[code[lasti]] in _APPELS


def _etat_et_cpu(tid: int | None) -> tuple[str, float] | None:
    """État ordonnanceur (`R` en cours/prêt, `S` endormi, `D` E/S disque…) et CPU
    cumulé (utime+stime, depuis la naissance du fil) d'un fil, lus dans
    `/proc/self/task/<tid>/stat`. None hors Linux ou si le fil vient de finir."""
    if not tid:
        return None
    try:
        with open(f"/proc/self/task/{tid}/stat", "rb") as f:
            brut = f.read()
    except OSError:
        return None  # fil fini entre l'instantané et la lecture, ou pas de /proc
    # Le nom du fil (champ 2) est entre parenthèses et peut contenir des espaces.
    champs = brut[brut.rfind(b")") + 2:].split()
    tic = os.sysconf("SC_CLK_TCK")
    return champs[0].decode(), (int(champs[11]) + int(champs[12])) / tic


def _chemin_court(chemin: str) -> str:
    """`oto_mcp/api/base.py`, `starlette/routing.py`, `selectors.py` : assez pour
    trouver le fichier, sans le préfixe de l'installation."""
    i = chemin.rfind("site-packages/")
    if i >= 0:
        return chemin[i + len("site-packages/"):]
    for borne in ("/oto_mcp/", "/tests/"):
        i = chemin.rfind(borne)
        if i >= 0:
            return chemin[i + 1:]
    return os.path.basename(chemin)


async def run_heartbeat_loop() -> None:
    """La boucle elle-même — déclarée dans `boucles_de_fond.BOUCLES` (`tiers=False` :
    aucun drain de travail en base, aucun effet chez un tiers, donc aucun risque de
    doublon prod/preprod) : elle tourne dans TOUS les environnements, y compris la
    préprod, exactement là où le prochain gel non identifié peut survenir. Son propre
    interrupteur (`enabled()`) vit ici, pas dans `boucles_de_fond.py`, qui se contente
    de le lire (`_hang_watch_armee`).

    Démarre le thread watchdog à l'entrée, l'arrête proprement à l'annulation
    (`finally`). Le corps de la boucle ne fait rien d'autre que battre le timestamp
    puis redormir — c'est la légèreté de CE geste qui est mesurée par
    `tests/test_hang_watch.py`, pas supposée."""
    try:
        interval = float(os.environ.get("OTO_SLOW_CALLBACK_WARN", "1.0") or "1.0")
        watch = HangWatch(interval=interval)
        watch.start()
    except Exception:
        logger.warning("hang watch : échec du démarrage du watchdog", exc_info=True)
        return
    logger.info(
        "hang watch actif (seuil %.1fs, plafond %d dump/min)",
        interval, _MAX_DUMPS_PER_MIN,
    )
    try:
        while True:
            watch.beat()
            await asyncio.sleep(interval / 2.0)
    finally:
        watch.stop()
