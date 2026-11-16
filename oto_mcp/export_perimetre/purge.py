"""Purger un tenant TIERS de la base partagée, une fois son périmètre parti ailleurs.

C'est l'export par périmètre (`docs/export-perimetre.md`) joué à l'envers : le même
classement (`classement.CLASSEMENT`, une règle par table, une table non classée refuse),
le même périmètre (`perimetre.resoudre` sur les orgs du tenant, avec ses refus), et les
mêmes prédicats compilés (`regles`). Ce qui partait est ici supprimé ; ce que l'export
laissait pour une raison écrite est tranché ci-dessous, table par table.

**Ce qui est supprimé** : les lignes du périmètre de toute table `possedee`, `indirecte`
ou `exclue` — sauf les `CONSERVEES` —, les dérivés du journal par org
(`DERIVES_DU_JOURNAL`), la ligne du tenant et tout ce qui la désigne.

**Ce qui est conservé** (`CONSERVEES`) : nos pièces — acceptations de nos documents
légaux, commerce et facturation. Le mode à blanc les compte à part, et la vérification
de fin les exclut.

**Ce qui n'est pas touché** : les tables `instance` sans propriétaire (registres, caches,
barèmes, semis de l'instance), et toute ligne hors périmètre — une table partagée par
plusieurs tenants (le journal `tool_calls`, les disponibilités de connecteurs, les nœuds
et guides de plateforme) n'est jamais vidée : chaque `DELETE` porte le prédicat de la
ligne, jamais un `TRUNCATE`.

**Refus, avant toute écriture, tous nommés** (`PurgeRefusee`, code de sortie 2) :

- ceux du périmètre de l'export (`perimetre.resoudre`) : un compte du tenant aussi membre
  d'une org hors périmètre, un compte qui n'est pas du tenant dans une de ses orgs, un
  tenant qui héberge aussi d'autres orgs ;
- `tenant_primaire` : le tenant est la ligne 1, celui de l'instance ;
- `partage_vers_tiers` : une ressource du tenant partagée (`destinataire` du classement)
  à une org, une équipe ou un compte hors du tenant ;
- `copie_par_un_tiers` : une entrée de la bibliothèque publique écrite par une autre org
  à partir d'une org du tenant ;
- `reference_entrante` : une ligne qui RESTE désigne par clé étrangère une ligne
  supprimée — une pièce conservée qu'un `ON DELETE CASCADE` emporterait, une page d'une
  autre org qui pointe vers une page du tenant.

**Un seul geste, vérifié avant le COMMIT** (`appliquer`) : une transaction
`REPEATABLE READ`. Les mêmes comptes et les mêmes refus sont rejoués dans l'instantané qui
écrit ; chaque table est vidée de ses lignes, enfants avant parents, et le nombre de lignes
supprimées doit égaler le compte ; puis tout est recompté et doit valoir 0. Le moindre
écart annule tout. Rejoué, le geste ne trouve plus rien et ne fait rien.

Les objets du stockage ne sont pas supprimés ici : `cles_d_objet` les relève AVANT la
purge (après, plus rien ne les désigne), pour qu'ils soient retirés à part.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable
from urllib.parse import quote

from .classement import CLASSEMENT, INSTANCE, Table
from .decouverte import Schema, lire_schema, verifier_classement
from .objets import COLONNES_D_URL, COLONNES_DE_CLES, cle_du_chemin
from .perimetre import Perimetre, prefixes_tiers, resoudre
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


class PurgeRefusee(RuntimeError):
    """La purge ne peut pas partir. `code` nomme le refus ; rien n'a été écrit."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code} : {message}")


@dataclass
class Plan:
    """Ce que la purge ferait : lignes à supprimer et lignes conservées, par table."""
    tenant: str
    id_tenant: int | None
    orgs: tuple[int, ...]
    comptes: int
    supprimer: dict[str, int] = field(default_factory=dict)
    conserver: dict[str, int] = field(default_factory=dict)

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


def _regle(table: str, entree: Table) -> Regle | None:
    if table in DERIVES_DU_JOURNAL:
        return DERIVES_DU_JOURNAL[table]
    return None if entree.classe == INSTANCE else entree.regle


def compilateur(classement: dict[str, Table]) -> Callable[[str], str]:
    """`pred(table)` : « cette ligne est du tenant ». La règle brute de l'export (sans la
    règle des anciens comptes : une ligne d'une org du tenant part avec elle, quel que soit
    le compte qui l'a écrite). Un PARTAGE (`destinataire`) est du tenant si sa ressource
    OU son destinataire l'est — l'export, lui, exige les deux : ici, un partage vers un
    compte qui disparaît disparaît avec lui (le cas inverse, une ressource du tenant
    partagée à un tiers, refuse : `refus`)."""
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


def _perimetre(conn, slug: str) -> Perimetre | None:
    ligne = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if ligne is None:
        return None
    if ligne["id"] == 1:
        raise PurgeRefusee("tenant_primaire", f"le tenant {slug!r} est la ligne 1, celui de "
                           "l'instance : il ne se purge pas")
    orgs = [r["id"] for r in conn.execute(
        "SELECT id FROM orgs WHERE tenant_id = %s ORDER BY id", (ligne["id"],))]
    if orgs:
        return resoudre(conn, orgs)
    # Le tenant n'a plus d'org : restent sa ligne et les comptes qu'il qualifie.
    subs = tuple(r["sub"] for r in conn.execute(
        "SELECT sub FROM users WHERE left(sub, length(%(p)s)) = %(p)s ORDER BY sub",
        {"p": f"{slug}:"}))
    return Perimetre((), (), (), subs, ligne["id"], slug, False, prefixes_tiers(conn))


def lire(conn, slug: str) -> Lecture | None:
    """Compile la lecture du tenant `slug`, ou `None` s'il n'existe plus."""
    perimetre = _perimetre(conn, slug)
    if perimetre is None:
        return None
    schema = lire_schema(conn)
    classement = verifier_classement(schema, CLASSEMENT)
    inconnues = sorted((set(CONSERVEES) | set(DERIVES_DU_JOURNAL)) - set(classement))
    if inconnues:
        raise PurgeRefusee("classement", f"tables nommées par la purge, absentes du "
                           f"classement : {inconnues}")
    pred = compilateur(classement)
    ecrites = {t for t in classement
               if t not in CONSERVEES and _regle(t, classement[t]) is not None}
    return Lecture(schema, classement, perimetre, pred, perimetre.parametres(),
                   tuple(ordre_enfants_d_abord(schema, classement, ecrites)),
                   tuple(sorted(t for t in CONSERVEES if t in classement)))


def _compte(conn, sql: str, params: dict) -> int:
    return int(conn.execute(sql, params).fetchone()["n"])


def refus(conn, lu: Lecture) -> None:
    """Lève `PurgeRefusee` (le premier refus rencontré, avec tous ses cas comptés)."""
    p = lu.params
    partages = {}
    for t, e in sorted(lu.classement.items()):
        if e.destinataire is None or t not in lu.ecrites:
            continue
        # Le destinataire `platform` est « tout le monde » (`grants_chain.EVERYONE`) : ce
        # n'est pas un tiers, l'arête part avec la ressource.
        ressource, dest = e.regle.predicat(lu.pred), e.destinataire.predicat(lu.pred)
        n = _compte(conn, f"SELECT count(*) AS n FROM {t} WHERE ({ressource}) AND NOT "
                          f"({dest}) AND {e.destinataire.colonne_type} <> 'platform'", p)
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


def compter(conn, lu: Lecture) -> Plan:
    p = lu.perimetre
    plan = Plan(p.tenant_slug, p.id_tenant, p.orgs, len(p.subs))
    for t in lu.ecrites:
        plan.supprimer[t] = _compte(conn, f"SELECT count(*) AS n FROM {t} WHERE {lu.pred(t)}",
                                    lu.params)
    for t in lu.conservees:
        plan.conserver[t] = _compte(
            conn, f"SELECT count(*) AS n FROM {t} WHERE {lu.classement[t].regle.predicat(lu.pred)}",
            lu.params)
    return plan


def a_blanc(conn, slug: str) -> Plan:
    """Compte ce que la purge supprimerait et ce qu'elle garderait, et joue ses refus.
    N'écrit rien : tourne sur une connexion en lecture seule."""
    lu = lire(conn, slug)
    if lu is None:
        return Plan(slug, None, (), 0)
    refus(conn, lu)
    return compter(conn, lu)


def cles_d_objet(conn, slug: str, base_publique: str) -> tuple[set[str], set[str]]:
    """(clés, préfixes) des objets du stockage que le tenant désigne : les colonnes de clé
    et d'URL de notre stockage public des lignes purgées, et le préfixe `images/<sub>/`
    de chaque compte (images déposées par un agent, que seule une URL dans un contenu
    désigne). À relever AVANT la purge."""
    lu = lire(conn, slug)
    if lu is None:
        return set(), set()
    cles: set[str] = set()
    for nom in sorted(COLONNES_DE_CLES | {"copilot_attachments.s3_key"}):
        t, col = nom.split(".")
        if t in lu.ecrites:
            cles |= {r["v"] for r in conn.execute(
                f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({lu.pred(t)}) "
                f"AND {col} IS NOT NULL", lu.params)}
    prefixe = base_publique.rstrip("/") + "/"
    for nom in sorted(COLONNES_D_URL):
        t, col = nom.split(".")
        if t in lu.ecrites:
            cles |= {cle_du_chemin(r["v"][len(prefixe):]) for r in conn.execute(
                f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({lu.pred(t)}) "
                f"AND {col} LIKE %(prefixe)s", {**lu.params, "prefixe": prefixe + "%"})}
    prefixes = {f"images/{quote(sub, safe='')}/" for sub in lu.perimetre.subs}
    return cles, prefixes


class PurgeIncoherente(RuntimeError):
    """Un compte relu dans la transaction ne tombe pas juste : tout est annulé."""


EN_LOTS = ("tool_calls",)
"""Les tables trop lourdes pour la transaction finale : le journal d'un tenant pèse plus
d'un million de lignes. Elles se vident d'abord par lots (`vider_par_lots`)."""
LOT_D_IDS = 50_000


def vider_par_lots(conn, slug: str, table: str = "tool_calls", lot: int = LOT_D_IDS,
                   rapport: Callable[[int, int], None] | None = None) -> int:
    """Vide `table` des lignes du périmètre par tranches d'ids, une transaction courte par
    tranche (autocommit) : chaque tranche ne verrouille que ses lignes, le WAL s'écoule au
    fil de l'eau, aucune transaction longue ne retient le nettoyage de la base.

    Les refus sont joués AVANT la première tranche : un tenant refusé ne perd rien. Une
    tranche est idempotente (elle porte le prédicat) ; interrompu, le geste se rejoue. Les
    lignes écrites pendant le passage partent dans la transaction finale (`appliquer`),
    qui recompte tout à 0 avant son COMMIT. Rend le nombre de lignes supprimées."""
    if table not in EN_LOTS:
        raise ValueError(f"{table} ne se vide pas par lots")
    conn.autocommit = True
    lu = lire(conn, slug)
    if lu is None:
        return 0
    refus(conn, lu)
    bornes = conn.execute(f"SELECT min(id) AS a, max(id) AS b FROM {table}").fetchone()
    if bornes["a"] is None:
        return 0
    total = 0
    for debut in range(bornes["a"], bornes["b"] + 1, lot):
        total += conn.execute(
            f"DELETE FROM {table} WHERE id >= %(debut)s AND id < %(fin)s AND ({lu.pred(table)})",
            {**lu.params, "debut": debut, "fin": debut + lot}).rowcount
        if rapport:
            rapport(debut + lot - bornes["a"], bornes["b"] - bornes["a"] + 1)
    return total


def journal_par_mois(conn, slug: str) -> dict[str, int]:
    """Le journal du périmètre encore en base, par mois calendaire (UTC) : à comparer, mois
    par mois, avec l'instance d'arrivée et avec les archives froides."""
    lu = lire(conn, slug)
    if lu is None:
        return {}
    return {r["mois"]: r["n"] for r in conn.execute(
        "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM') AS mois, count(*) AS n "
        f"FROM tool_calls WHERE {lu.pred('tool_calls')} GROUP BY 1 ORDER BY 1", lu.params)}


def appliquer(conn, slug: str) -> Plan:
    """La purge, en UNE transaction : refus, comptes, suppressions vérifiées, résidu nul.
    Rend le plan exécuté (vide si le tenant n'est plus là)."""
    import psycopg
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with conn.transaction():
        lu = lire(conn, slug)
        if lu is None:
            return Plan(slug, None, (), 0)
        refus(conn, lu)
        plan = compter(conn, lu)
        for t in lu.ecrites:
            n = conn.execute(f"DELETE FROM {t} WHERE {lu.pred(t)}", lu.params).rowcount
            if n != plan.supprimer[t]:
                raise PurgeIncoherente(f"{t} : {n} ligne(s) supprimée(s) pour "
                                       f"{plan.supprimer[t]} comptée(s) — tout est annulé")
        reste = {t: n for t, n in compter(conn, lu).supprimer.items() if n}
        if reste:
            raise PurgeIncoherente(f"résidu après purge : {reste} — tout est annulé")
        restants = _compte(conn, "SELECT count(*) AS n FROM users WHERE sub = ANY(%(subs)s)",
                           lu.params)
        if restants:
            raise PurgeIncoherente(f"{restants} compte(s) du tenant encore là — tout est annulé")
    return plan
