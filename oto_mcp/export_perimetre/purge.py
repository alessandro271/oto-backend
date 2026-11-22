"""Purger un tenant TIERS de la base partagée, une fois son périmètre parti ailleurs.

C'est l'export par périmètre (`docs/export-perimetre.md`) joué à l'envers : le même
classement (`classement.CLASSEMENT`, une règle par table, une table non classée refuse),
le même périmètre (`perimetre.resoudre` sur les orgs du tenant, avec ses refus), et les
mêmes prédicats compilés (`regles`). Le plan d'exécution est `docs/purge-de-tenant.md`.

**Le périmètre est FIGÉ** (`Perimetre`, sérialisé par `en_dict` / `de_dict`) : relevé une
fois, gardé, puis comparé à une lecture fraîche tant que le tenant existe
(`verifier_fige`) ; le moindre écart refuse (`releve_perime`). Le journal et le recompte
final lisent le périmètre figé, qui désigne encore ce que la base ne désigne plus. Les
comptes du tenant sans appartenance (`<slug>:x` d'un ancien membre) en font partie.

**Ce qui est supprimé** : les lignes du périmètre de toute table `possedee`, `indirecte`
ou `exclue` — sauf les `CONSERVEES` —, les dérivés du journal par org
(`DERIVES_DU_JOURNAL`), la ligne du tenant et tout ce qui la désigne. Les appels de nos
administrateurs dans une org du tenant partent avec elle (leurs arguments portent des
données du tenant) ; ils sont comptés à part (`appels_de_support`).

**Ce qui est conservé** (`CONSERVEES`) : nos pièces — acceptations de nos documents
légaux, commerce et facturation. Une pièce qu'un `ON DELETE CASCADE` depuis `orgs`
emporterait fait refuser (`reference_entrante`) : la conserver demande une autre décision.

**Ce qui n'est pas touché** : les tables `instance` sans propriétaire, et toute ligne hors
périmètre — chaque `DELETE` porte le prédicat de la ligne, jamais un `TRUNCATE`.

**Refus, avant toute écriture, tous nommés** (`PurgeRefusee`, code de sortie 2) : ceux du
périmètre de l'export ; `tenant_primaire` ; `releve_perime` ; `partage_vers_tiers` (une
ressource du tenant partagée à une org, une équipe ou un compte hors du tenant, ou à un
destinataire sans type ; un compte de messagerie du tenant prêté à un compte ou une
équipe hors du tenant) ; `copie_par_un_tiers` ; `reference_entrante`.

**L'ordre** : la base d'abord, en UNE transaction (`appliquer`), tout SAUF le journal ;
COMMIT seulement si le résidu vaut 0. Un refus y laisse donc TOUT intact. Puis le journal
par tranches d'ids (`vider_journal`), depuis le périmètre figé, recompté à 0. Puis le
recompte de toutes les tables, dans une nouvelle transaction (`recompter`).

La règle du journal est UNE : `JOURNAL_REGLE`, celle du classement pour `tool_calls`
(vérifiée), compilée en SQL pour la base et en Python (`classeur_du_journal`) pour les
archives froides — elles retirent exactement ce que la base supprime.
"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterator
from urllib.parse import quote

from .classement import CLASSEMENT, INSTANCE, Table
from .decouverte import Schema, lire_schema, verifier_classement
from .objets import COLONNES_D_URL, COLONNES_DE_CLES, cle_du_chemin
from .perimetre import ComptesHorsTenant, Perimetre, prefixes_tiers, resoudre
from .regles import Ou, ParOrg, ParSubSansOrg, Regle, vias

# Nos pièces : gardées, comptées à part, exclues de la vérification de fin.
CONSERVEES: dict[str, str] = {
    "legal_acceptances": "acceptation de NOS documents légaux",
    "legal_acceptance_events": "preuve d'acceptation de NOS documents légaux",
    "org_subscriptions": "pièce comptable (commerce)",
    "billing_payments": "pièce comptable (commerce)",
    "billing_invoices": "pièce comptable (commerce)",
    "billing_identities": "pièce comptable : l'identité figure sur les factures émises",
    "billing_contracts": "pièce comptable (commerce)",
    "credit_payer_modes": "qui paie dans l'org : versé par notre commerce",
}

# Les tables `instance` qui portent pourtant des lignes d'une org : les totaux du journal,
# DÉRIVÉS de lui. Le journal supprimé, leurs lignes ne décrivent plus rien.
DERIVES_DU_JOURNAL: dict[str, Regle] = {
    "journal_totaux_jour": Ou((ParOrg(), ParSubSansOrg())),
    "journal_jobs_jour": ParOrg(),
}

# LA règle du journal : celle du classement pour `tool_calls` (vérifiée par `lecture`),
# compilée en SQL pour la base et en Python pour les archives (`classeur_du_journal`).
JOURNAL = "tool_calls"
JOURNAL_REGLE: Regle = Ou((ParOrg(), ParSubSansOrg()))
LOT_D_IDS = 50_000

# Les prêts d'un compte de messagerie : le propriétaire est la ressource, le bénéficiaire
# (compte ou équipe) est le destinataire. Hors du tenant, refus.
PRETS_DE_COMPTE: dict[str, str] = {
    "connector_account_grants": "NOT (grantee_sub = ANY(%(subs)s))",
    "connector_account_group_grants": "NOT (grantee_group_id = ANY(%(groupes)s))",
}


class PurgeRefusee(RuntimeError):
    """La purge ne peut pas partir. `code` nomme le refus ; rien n'a été écrit."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code} : {message}")


class PurgeIncoherente(RuntimeError):
    """Un compte relu ne tombe pas juste : la transaction en cours est annulée."""


@dataclass
class Plan:
    """Ce que la purge ferait (ou a fait) : lignes par table, supprimées et conservées."""
    tenant: str
    id_tenant: int | None
    orgs: tuple[int, ...]
    comptes: int
    supprimer: dict[str, int] = field(default_factory=dict)
    conserver: dict[str, int] = field(default_factory=dict)
    appels_de_support: int = 0

    @property
    def vide(self) -> bool:
        return not any(self.supprimer.values())


@dataclass(frozen=True)
class Lecture:
    schema: Schema
    classement: dict[str, Table]
    perimetre: Perimetre
    pred: Callable[[str], str]
    params: dict
    ecrites: tuple[str, ...]        # tables dont des lignes se suppriment, enfants d'abord
    conservees: tuple[str, ...]


# ── le périmètre figé ────────────────────────────────────────────────────────────────

def en_dict(p: Perimetre) -> dict:
    return {k: list(v) if isinstance(v, tuple) else v
            for k, v in dataclasses.asdict(p).items()}


def de_dict(d: dict) -> Perimetre:
    return Perimetre(**{f.name: tuple(d[f.name]) if isinstance(d[f.name], list)
                        else d[f.name] for f in dataclasses.fields(Perimetre)})


def _perimetre(conn, slug: str) -> Perimetre | None:
    """Le périmètre FRAIS du tenant `slug`, ou `None` s'il n'existe plus."""
    ligne = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if ligne is None:
        return None
    if ligne["id"] == 1:
        raise PurgeRefusee("tenant_primaire", f"le tenant {slug!r} est la ligne 1, celui de "
                           "l'instance : il ne se purge pas")
    from ..db.tenants import orgs_du_tenant
    orgs = orgs_du_tenant(conn, ligne["id"])
    p = (resoudre(conn, orgs) if orgs else
         Perimetre((), (), (), (), ligne["id"], slug, False, prefixes_tiers(conn)))
    # Les comptes du tenant sans appartenance (anciens membres) : à lui aussi.
    autres = [r["sub"] for r in conn.execute(
        "SELECT sub FROM users WHERE left(sub, length(%(p)s)) = %(p)s "
        "AND NOT (sub = ANY(%(subs)s)) ORDER BY sub", {"p": f"{slug}:", "subs": list(p.subs)})]
    if autres:
        ailleurs = conn.execute(
            "SELECT count(*) AS n FROM org_members WHERE sub = ANY(%(a)s) "
            "AND NOT (org_id = ANY(%(o)s))", {"a": autres, "o": list(p.orgs)}).fetchone()["n"]
        if ailleurs:
            raise ComptesHorsTenant(f"{ailleurs} appartenance(s) de comptes du tenant "
                                    f"{slug!r} à des orgs hors de son périmètre")
        p = dataclasses.replace(p, subs=tuple(sorted({*p.subs, *autres})))
    return p


def ecarts(fige: Perimetre, frais: Perimetre) -> dict[str, list]:
    """Ce qui distingue le périmètre figé d'une lecture fraîche (vide : identiques)."""
    out = {}
    for nom in ("orgs", "groupes", "subs"):
        a, b = set(getattr(fige, nom)), set(getattr(frais, nom))
        if a != b:
            out[nom] = {"en_trop": len(a - b), "nouveaux": len(b - a)}
    if fige.id_tenant != frais.id_tenant:
        out["id_tenant"] = [fige.id_tenant, frais.id_tenant]
    return out


def verifier_fige(conn, fige: Perimetre) -> bool:
    """Lève `releve_perime` si le tenant existe encore et que son périmètre a bougé
    depuis le relevé. Rend `False` si le tenant n'existe plus (base déjà purgée)."""
    frais = _perimetre(conn, fige.tenant_slug)
    if frais is None:
        return False
    e = ecarts(fige, frais)
    if e:
        raise PurgeRefusee("releve_perime", f"le périmètre a changé depuis le relevé : {e} "
                           "— refaire le relevé (nouveau dossier) et le compte à blanc")
    return True


# ── la lecture ───────────────────────────────────────────────────────────────────────

def _regle(table: str, entree: Table) -> Regle | None:
    if table in DERIVES_DU_JOURNAL:
        return DERIVES_DU_JOURNAL[table]
    return None if entree.classe == INSTANCE else entree.regle


def compilateur(classement: dict[str, Table]) -> Callable[[str], str]:
    """`pred(table)` : « cette ligne est du tenant ». La règle brute de l'export (sans la
    règle des anciens comptes : une ligne d'une org du tenant part avec elle, quel que soit
    le compte qui l'a écrite). Un PARTAGE (`destinataire`) est du tenant si sa ressource
    OU son destinataire l'est : un partage reçu d'un tiers part avec son destinataire (le
    cas inverse refuse : `refus`)."""
    @lru_cache(maxsize=None)
    def pred(table: str) -> str:
        entree = classement[table]
        regle = _regle(table, entree)
        if regle is None:
            return "FALSE"
        p = regle.predicat(pred)
        if regle is entree.regle and entree.destinataire is not None:
            p = f"(({p}) OR ({entree.destinataire.predicat(pred)}))"
        return p
    return pred


def ordre_enfants_d_abord(schema: Schema, classement: dict[str, Table],
                          tables: set[str]) -> list[str]:
    """Les `tables`, chaque enfant (clé étrangère ou lien `Via`) avant son parent."""
    deps = {t: ({k.table for k in schema.cles if k.cible == t and k.table in tables}
                | {e for e in tables if t in {v.parent for v in vias(
                    _regle(e, classement[e]) or Ou(()))}}) - {t}
            for t in tables}
    ordre: list[str] = []
    while deps:
        prets = sorted(t for t, d in deps.items() if not d - set(ordre))
        if not prets:
            raise RuntimeError(f"cycle de dépendances entre tables purgées : {sorted(deps)}")
        ordre.extend(prets)
        for t in prets:
            del deps[t]
    return ordre


def lecture(conn, perimetre: Perimetre) -> Lecture:
    """Compile la lecture d'un périmètre (frais ou figé)."""
    schema = lire_schema(conn)
    classement = verifier_classement(schema, CLASSEMENT)
    inconnues = sorted((set(CONSERVEES) | set(DERIVES_DU_JOURNAL) | set(PRETS_DE_COMPTE)
                        | {JOURNAL}) - set(classement))
    if inconnues:
        raise PurgeRefusee("classement", f"tables nommées par la purge, absentes du "
                           f"classement : {inconnues}")
    if classement[JOURNAL].regle != JOURNAL_REGLE:
        raise PurgeRefusee("classement", f"la règle de {JOURNAL} n'est plus celle que les "
                           "archives appliquent (`JOURNAL_REGLE`) : les accorder d'abord")
    pred = compilateur(classement)
    ecrites = {t for t in classement
               if t not in CONSERVEES and _regle(t, classement[t]) is not None}
    return Lecture(schema, classement, perimetre, pred, perimetre.parametres(),
                   tuple(ordre_enfants_d_abord(schema, classement, ecrites)),
                   tuple(sorted(t for t in CONSERVEES if t in classement)))


def lire(conn, slug: str) -> Lecture | None:
    """La lecture du périmètre FRAIS du tenant `slug`, ou `None` s'il n'existe plus."""
    perimetre = _perimetre(conn, slug)
    return None if perimetre is None else lecture(conn, perimetre)


@contextmanager
def en_lecture_seule(conn) -> Iterator[None]:
    """Une transaction `READ ONLY` explicite, en un seul instantané : à blanc, rien ne
    peut s'écrire, quel que soit le mode de la connexion."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def _compte(conn, sql: str, params: dict) -> int:
    return int(conn.execute(sql, params).fetchone()["n"])


# ── les refus ────────────────────────────────────────────────────────────────────────

def refus(conn, lu: Lecture) -> None:
    """Lève `PurgeRefusee` (le premier refus rencontré, avec tous ses cas comptés)."""
    p = lu.params
    partages = {}
    for t, e in sorted(lu.classement.items()):
        if e.destinataire is None or t not in lu.ecrites:
            continue
        # Le destinataire `platform` est « tout le monde » (`grants_chain.EVERYONE`) : ce
        # n'est pas un tiers, l'arête part avec la ressource. Un type NULL n'en est pas un.
        ressource, dest = e.regle.predicat(lu.pred), e.destinataire.predicat(lu.pred)
        n = _compte(conn, f"SELECT count(*) AS n FROM {t} WHERE ({ressource}) AND NOT "
                          f"COALESCE(({dest}), FALSE) AND "
                          f"{e.destinataire.colonne_type} IS DISTINCT FROM 'platform'", p)
        if n:
            partages[t] = n
    for t, hors in PRETS_DE_COMPTE.items():
        if t in lu.ecrites:
            n = _compte(conn, f"SELECT count(*) AS n FROM {t} WHERE ({lu.pred(t)}) AND "
                              f"COALESCE({hors}, TRUE)", p)
            if n:
                partages[t] = n
    if partages:
        raise PurgeRefusee("partage_vers_tiers", f"des ressources du tenant sont partagées à "
                           f"un tiers (org, équipe ou compte hors tenant) : {partages}")
    biblio = lu.schema.vues.get("guide_library", "guide_library")
    if biblio in lu.ecrites:
        n = _compte(conn, f"SELECT count(*) AS n FROM {biblio} WHERE source_org_id = "
                          "ANY(%(orgs)s) AND NOT (author_org_id = ANY(%(orgs)s))", p)
        if n:
            raise PurgeRefusee("copie_par_un_tiers", f"{n} entrée(s) de la bibliothèque "
                               "publique écrites par une autre org à partir d'une org du "
                               "tenant : elles ne sont pas au tenant")
    entrantes = references_entrantes(conn, lu)
    if entrantes:
        raise PurgeRefusee("reference_entrante", "des lignes qui restent désignent des "
                           f"lignes supprimées (table.colonnes → table : lignes) : {entrantes}")


def references_entrantes(conn, lu: Lecture) -> dict[str, int]:
    """Les lignes qui RESTENT et désignent par clé étrangère une ligne supprimée."""
    ecrites = set(lu.ecrites)
    out: dict[str, int] = {}
    for k in lu.schema.cles:
        if k.cible not in ecrites:
            continue
        reste = f"NOT ({lu.pred(k.table)})" if k.table in ecrites else "TRUE"
        sql = (f"SELECT count(*) AS n FROM {k.table} WHERE {reste} AND "
               f"({', '.join(k.colonnes)}) IN (SELECT {', '.join(k.colonnes_cible)} "
               f"FROM {k.cible} WHERE {lu.pred(k.cible)})")
        n = _compte(conn, sql, lu.params)
        if n:
            out[f"{k.table}.{','.join(k.colonnes)} → {k.cible}"] = n
    return out


# ── compter ──────────────────────────────────────────────────────────────────────────

def compter(conn, lu: Lecture, sauf: tuple[str, ...] = ()) -> Plan:
    p = lu.perimetre
    plan = Plan(p.tenant_slug, p.id_tenant, p.orgs, len(p.subs))
    for t in lu.ecrites:
        if t not in sauf:
            plan.supprimer[t] = _compte(
                conn, f"SELECT count(*) AS n FROM {t} WHERE {lu.pred(t)}", lu.params)
    for t in lu.conservees:
        plan.conserver[t] = _compte(
            conn, f"SELECT count(*) AS n FROM {t} WHERE "
                  f"{lu.classement[t].regle.predicat(lu.pred)}", lu.params)
    if JOURNAL not in sauf:
        plan.appels_de_support = _compte(
            conn, f"SELECT count(*) AS n FROM {JOURNAL} WHERE org_id = ANY(%(orgs)s) "
                  "AND NOT (COALESCE(sub, '') = ANY(%(subs)s))", lu.params)
    return plan


def a_blanc(conn, slug: str) -> Plan:
    """Compte ce que la purge supprimerait et garderait, et joue ses refus. À appeler
    dans `en_lecture_seule`."""
    lu = lire(conn, slug)
    if lu is None:
        return Plan(slug, None, (), 0)
    refus(conn, lu)
    return compter(conn, lu)


def recompter(conn, fige: Perimetre) -> Plan:
    """Le recompte depuis le périmètre FIGÉ, pour TOUTES les tables purgées (attendu :
    0 partout), et les conservées. À appeler dans `en_lecture_seule`, après le COMMIT."""
    lu = lecture(conn, fige)
    plan = compter(conn, lu)
    plan.supprimer["tenants"] = _compte(conn, "SELECT count(*) AS n FROM tenants WHERE "
                                              "slug = %(s)s", {"s": fige.tenant_slug})
    plan.supprimer["users (préfixe du tenant)"] = _compte(
        conn, "SELECT count(*) AS n FROM users WHERE left(sub, length(%(p)s)) = %(p)s",
        {"p": f"{fige.tenant_slug}:"})
    return plan


def journal_par_mois(conn, slug: str) -> dict[str, int]:
    """Le journal du périmètre encore en base, par mois calendaire (UTC)."""
    lu = lire(conn, slug)
    if lu is None:
        return {}
    return {r["mois"]: r["n"] for r in conn.execute(
        "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM') AS mois, count(*) AS n "
        f"FROM {JOURNAL} WHERE {lu.pred(JOURNAL)} GROUP BY 1 ORDER BY 1", lu.params)}


def classeur_du_journal(p: Perimetre, regle: Regle = JOURNAL_REGLE
                        ) -> tuple[tuple[str, ...], Callable[[dict[str, str]], bool]]:
    """`JOURNAL_REGLE` compilée en Python, pour un enregistrement CSV de `COPY` (champ
    vide = NULL) : (colonnes lues, « cet enregistrement est du tenant »)."""
    orgs, subs = {str(o) for o in p.orgs}, set(p.subs)

    def compiler(r: Regle) -> Callable[[dict[str, str]], bool]:
        if isinstance(r, ParOrg):
            return lambda e: e[r.colonne] in orgs
        if isinstance(r, ParSubSansOrg):
            return lambda e: e[r.colonne_org] == "" and e[r.colonne] in subs
        if isinstance(r, Ou):
            parties = [compiler(x) for x in r.regles]
            return lambda e: any(f(e) for f in parties)
        raise TypeError(f"règle du journal non compilable en Python : {r!r}")
    return tuple(dict.fromkeys(regle.colonnes())), compiler(regle)


# ── objets ───────────────────────────────────────────────────────────────────────────

def objets(conn, lu: Lecture, base_publique: str) -> tuple[set[str], set[str]]:
    """(préfixes de propriétaire, clés hors de ces préfixes) des objets du tenant.

    Une clé du stockage est `<famille>/<propriétaire>/…` (`media_store`) : sous le préfixe
    d'un propriétaire du périmètre, tout est à lui. Les clés que la base désigne et qui
    tombent HORS de ces préfixes sont rendues à part, pour examen : jamais supprimées."""
    q = lambda v: quote(str(v), safe="")  # noqa: E731
    ids = lambda t: [r["id"] for r in conn.execute(  # noqa: E731
        f"SELECT id FROM {t} WHERE {lu.pred(t)}", lu.params)]
    prefixes = ({f"{fam}/{q(s)}/" for s in lu.perimetre.subs
                 for fam in ("avatars", "images", "copilot-attachments")}
                | {f"org-logos/{q(o)}/" for o in lu.perimetre.orgs}
                | {f"{fam}/{q(i)}/" for i in ids("projects")
                   for fam in ("project-files", "transcription-jobs")}
                | {f"app-files/{q(i)}/" for i in ids("apps")})
    cles: set[str] = set()
    for nom in sorted(COLONNES_DE_CLES | {"copilot_attachments.s3_key"}):
        t, col = nom.split(".")
        if t in lu.ecrites:
            cles |= {r["v"] for r in conn.execute(
                f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({lu.pred(t)}) "
                f"AND {col} IS NOT NULL", lu.params)}
    racine = base_publique.rstrip("/") + "/"
    for nom in sorted(COLONNES_D_URL):
        t, col = nom.split(".")
        if t in lu.ecrites:
            cles |= {cle_du_chemin(r["v"][len(racine):]) for r in conn.execute(
                f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({lu.pred(t)}) "
                f"AND {col} LIKE %(r)s", {**lu.params, "r": racine + "%"})}
    return prefixes, {c for c in cles if not any(c.startswith(p) for p in prefixes)}


# ── écrire ───────────────────────────────────────────────────────────────────────────

def appliquer(conn, fige: Perimetre) -> Plan:
    """La base, en UNE transaction `REPEATABLE READ`, tout SAUF le journal : périmètre
    frais comparé au figé, refus, comptes, suppressions vérifiées, résidu nul, COMMIT.
    Le moindre écart annule tout. Rend le plan exécuté (vide si déjà fait)."""
    conn.autocommit = True
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if not verifier_fige(conn, fige):
            return Plan(fige.tenant_slug, None, (), 0)
        lu = lire(conn, fige.tenant_slug)
        refus(conn, lu)
        plan = compter(conn, lu, sauf=(JOURNAL,))
        for t in lu.ecrites:
            if t == JOURNAL:
                continue
            n = conn.execute(f"DELETE FROM {t} WHERE {lu.pred(t)}", lu.params).rowcount
            if n != plan.supprimer[t]:
                raise PurgeIncoherente(f"{t} : {n} ligne(s) supprimée(s) pour "
                                       f"{plan.supprimer[t]} comptée(s) — tout est annulé")
        reste = {t: n for t, n in compter(conn, lu, sauf=(JOURNAL,)).supprimer.items() if n}
        if reste:
            raise PurgeIncoherente(f"résidu après purge : {reste} — tout est annulé")
    return plan


def vider_journal(conn, fige: Perimetre, lot: int = LOT_D_IDS,
                  rapport: Callable[[int, int], None] | None = None) -> int:
    """Le journal du périmètre FIGÉ, par tranches d'ids : une courte transaction par
    tranche (verrous de ligne seulement, WAL au fil de l'eau, aucune transaction longue).
    Chaque tranche porte le prédicat : rejouée, elle ne trouve plus rien. Passe jusqu'à
    un recompte nul (au plus trois fois). Rend le nombre de lignes supprimées."""
    conn.autocommit = True
    lu = lecture(conn, fige)
    pred, total = lu.pred(JOURNAL), 0
    for _ in range(3):
        bornes = conn.execute(f"SELECT min(id) AS a, max(id) AS b FROM {JOURNAL}").fetchone()
        if bornes["a"] is not None:
            for debut in range(bornes["a"], bornes["b"] + 1, lot):
                total += conn.execute(
                    f"DELETE FROM {JOURNAL} WHERE id >= %(d)s AND id < %(f)s AND ({pred})",
                    {**lu.params, "d": debut, "f": debut + lot}).rowcount
                if rapport:
                    rapport(debut + lot - bornes["a"], bornes["b"] - bornes["a"] + 1)
        if not _compte(conn, f"SELECT count(*) AS n FROM {JOURNAL} WHERE {pred}", lu.params):
            return total
    raise PurgeIncoherente(f"{JOURNAL} : des lignes du périmètre s'écrivent encore")
