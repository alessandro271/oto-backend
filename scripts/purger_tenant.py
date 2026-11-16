"""Purger un tenant tiers parti sur sa propre instance : base, objets, archives du journal.

Le plan d'exécution est `docs/purge-de-tenant.md` ; chaque geste est décrit dans son
module (`oto_mcp/export_perimetre/purge*.py`). Tout est À BLANC par défaut.

    purger_tenant.py tout --tenant <slug> [--dossier D]              # comptes 1, 2, 3
    purger_tenant.py tout --tenant <slug> --dossier D --appliquer \\
        [--extraction-faite E]                                       # le passage entier
    purger_tenant.py base --tenant <slug> [--appliquer]              # 1. la base
    purger_tenant.py objets --tenant <slug> --dossier D [--appliquer] # 2. le stockage
    purger_tenant.py archives --tenant <slug> --dossier D \\
        [--extraire E | --appliquer --extrait E]                     # 3. les archives
    purger_tenant.py extraire --tenant <slug> --dossier D --depuis <instant>
        # la remise au titulaire, sur décision : elle lit du CONTENU

`--dossier` garde l'état d'un passage (`perimetre.json`, `objets.json`, `etat.json`,
`constat.json`) : le périmètre et les objets se relèvent AVANT la purge de la base, et
servent encore après. Une étape faite n'est pas rejouée ; rejouée, chacune ne trouve
plus rien. L'environnement est celui du backend (`DATABASE_URL`, `OTO_MCP_S3_*`) ; sur la
box, par le lanceur (`deploy/lanceur_secrets.py --script scripts/purger_tenant.py …`).
Un refus s'imprime nommé et sort en code 2, sans rien écrire.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_RACINE))

from oto_mcp.export_perimetre import (purge, purge_archives, purge_objets,  # noqa: E402
                                      purge_remise)
from oto_mcp.export_perimetre.perimetre import PerimetreRefuse  # noqa: E402

ETAPES = ("archives", "journal", "base", "objets")   # l'ordre du passage
ELEMENTS = ("base", "objets", "archives")             # ce que le constat vérifie
HORS_SCRIPT = {"annuaire": "la base d'annuaire (comptes de connexion) du tenant : autres "
                           "identifiants, jouée par l'infrastructure dans le même passage",
               "sauvegardes": "les sauvegardes de la base antérieures au passage : vérifier "
                              "leur expiration (infrastructure)"}


class EtapeEchouee(RuntimeError):
    pass


# ── accès ────────────────────────────────────────────────────────────────────────────

def _archivage():
    """`deploy/archive_tool_calls.py` : son client S3, son seau, son verrou."""
    chemin = _RACINE / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _connexion(lecture_seule: bool) -> psycopg.Connection:
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    conn.read_only = lecture_seule
    return conn


class _Verrou:
    """Le verrou de l'archivage quotidien du journal : ils ne se croisent jamais."""

    def __init__(self, conn, mod):
        self.conn, self.cle = conn, mod.LOCK_KEY

    def __enter__(self):
        if not self.conn.execute("SELECT pg_try_advisory_lock(%s) AS ok",
                                 (self.cle,)).fetchone()["ok"]:
            raise EtapeEchouee("l'archivage du journal tient son verrou : rejouer plus tard")

    def __exit__(self, *_):
        self.conn.execute("SELECT pg_advisory_unlock(%s)", (self.cle,))


# ── l'état d'un passage ──────────────────────────────────────────────────────────────

def _lire_json(dossier: str | None, nom: str) -> dict | None:
    if not dossier or not os.path.exists(os.path.join(dossier, nom)):
        return None
    with open(os.path.join(dossier, nom), encoding="utf-8") as f:
        return json.load(f)


def _ecrire_json(dossier: str, nom: str, donnees: dict) -> None:
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, nom)
    with open(chemin + ".tmp", "w", encoding="utf-8") as f:
        json.dump(donnees, f, indent=2, ensure_ascii=False, default=str)
    os.replace(chemin + ".tmp", chemin)


def releve(conn, slug: str, dossier: str | None) -> dict:
    """Le périmètre et les objets du tenant, relevés une fois AVANT la purge de la base
    et gardés dans `dossier` : après elle, plus rien ne les désigne."""
    deja = _lire_json(dossier, "perimetre.json")
    if deja is not None:
        if deja["tenant"] != slug:
            raise EtapeEchouee(f"{dossier} est le passage du tenant {deja['tenant']!r}")
        return deja
    lu = purge.lire(conn, slug)
    if lu is None:
        raise EtapeEchouee(f"tenant {slug!r} absent et aucun relevé dans {dossier!r} : son "
                           "périmètre ne se lit plus")
    from oto_mcp import media_store
    cles, prefixes = purge.cles_d_objet(conn, slug, media_store.public_base())
    r = {"tenant": slug, "id": lu.perimetre.id_tenant, "orgs": list(lu.perimetre.orgs),
         "comptes": len(lu.perimetre.subs), "cles": sorted(cles), "prefixes": sorted(prefixes)}
    if dossier:
        _ecrire_json(dossier, "perimetre.json", r)
    return r


def _tenant_du_journal(r: dict) -> purge_archives.Tenant:
    return purge_archives.Tenant(f"{r['tenant']}:", frozenset(str(o) for o in r["orgs"]))


# ── les trois éléments ───────────────────────────────────────────────────────────────

def compte_base(conn, slug: str, r: dict) -> dict:
    """Le compte à blanc ; le tenant parti, ce qui en resterait encore : ses orgs relevées,
    ses comptes."""
    plan = purge.a_blanc(conn, slug)
    total = sum(plan.supprimer.values())
    if plan.id_tenant is None:
        total = conn.execute(
            "SELECT (SELECT count(*) FROM orgs WHERE id = ANY(%(o)s)) + (SELECT count(*) "
            "FROM users WHERE left(sub, length(%(p)s)) = %(p)s) AS n",
            {"o": r["orgs"], "p": f"{slug}:"}).fetchone()["n"]
    return {"present": plan.id_tenant is not None, "supprimer": plan.supprimer,
            "journal_par_mois": purge.journal_par_mois(conn, slug),
            "total": total, "conserver": plan.conserver}


def compte_objets(s3, bucket: str, r: dict) -> dict:
    return purge_objets.resume(purge_objets.inventaire(s3, bucket, set(r["cles"]),
                                                       set(r["prefixes"])))


def compte_archives(conn, s3, bucket: str, r: dict) -> dict:
    mois = purge_archives.compter(conn, s3, bucket, _tenant_du_journal(r))
    return {m.mois: {"cle": m.cle, "total": m.total, "tenant": m.tenant, "reste": m.reste,
                     "versions_anciennes": m.versions_anciennes} for m in mois}


def tout(args) -> int:
    mod = _archivage()
    bucket = mod._env("OTO_MCP_S3_BUCKET")
    s3 = mod._s3_client()
    if args.appliquer and not args.dossier:
        raise SystemExit("--appliquer exige --dossier : l'état du passage s'y garde")
    etat = _lire_json(args.dossier, "etat.json") or {"fait": {}}
    with _connexion(lecture_seule=not args.appliquer) as conn, _Verrou(conn, mod):
        r = releve(conn, args.tenant, args.dossier)
        avant = {"base": compte_base(conn, args.tenant, r),
                 "objets": compte_objets(s3, bucket, r),
                 "archives": compte_archives(conn, s3, bucket, r)}
        if not args.appliquer:
            print(json.dumps({"a_blanc": avant, "hors_script": HORS_SCRIPT}, indent=2,
                             ensure_ascii=False, default=str))
            print("\nÀ BLANC : rien n'a été écrit.")
            return 0
        etat.setdefault("avant", avant)
        etat["tenant"] = args.tenant
        tenant = _tenant_du_journal(r)
        try:
            if "archives" not in etat["fait"]:
                mois = purge_archives.compter(conn, s3, bucket, tenant)
                extrait = args.extraction_faite or os.path.join(args.dossier, "journal-archive")
                if not args.extraction_faite:
                    purge_archives.extraire(s3, bucket, tenant,
                                            [m for m in mois if m.tenant], extrait)
                etat["fait"]["archives"] = {"extrait": extrait, **purge_archives.reecrire(
                    conn, s3, bucket, tenant, mois, extrait)}
                _ecrire_json(args.dossier, "etat.json", etat)
            if "journal" not in etat["fait"]:
                etat["fait"]["journal"] = {"supprime": purge.vider_par_lots(
                    conn, args.tenant, rapport=_avancement)}
                _ecrire_json(args.dossier, "etat.json", etat)
            if "base" not in etat["fait"]:
                conn.autocommit = False
                plan = purge.appliquer(conn, args.tenant)
                conn.autocommit = True
                etat["fait"]["base"] = {"supprime": sum(plan.supprimer.values())
                                        + etat["fait"]["journal"]["supprime"]}
                _ecrire_json(args.dossier, "etat.json", etat)
            if "objets" not in etat["fait"]:
                versions = purge_objets.inventaire(s3, bucket, set(r["cles"]),
                                                   set(r["prefixes"]))
                etat["fait"]["objets"] = {"supprime": purge_objets.supprimer(s3, bucket,
                                                                             versions)}
                _ecrire_json(args.dossier, "etat.json", etat)
        except Exception as e:
            etat["echec"] = {"etape": next(x for x in ETAPES if x not in etat["fait"]),
                             "erreur": f"{type(e).__name__} : {e}"}
            _ecrire_json(args.dossier, "etat.json", etat)
            print(json.dumps(etat, indent=2, ensure_ascii=False, default=str), file=sys.stderr)
            raise
        etat.pop("echec", None)
        apres = {"base": compte_base(conn, args.tenant, r),
                 "objets": compte_objets(s3, bucket, r),
                 "archives": compte_archives(conn, s3, bucket, r)}
    constat = _constat(etat, apres)
    _ecrire_json(args.dossier, "constat.json", constat)
    print(json.dumps(constat, indent=2, ensure_ascii=False, default=str))
    print(_constat_texte(constat))
    return 0 if constat["complet"] else 1


def _avancement(fait: int, total: int) -> None:
    print(f"  journal : ids {min(fait, total)}/{total}", file=sys.stderr, flush=True)


def _constat(etat: dict, apres: dict) -> dict:
    av = etat["avant"]
    reste_archives = sum(m["tenant"] for m in apres["archives"].values())
    elements = {
        "base": {"avant": av["base"]["total"], "supprime": etat["fait"]["base"]["supprime"],
                 "reste": apres["base"]["total"], "conserve": apres["base"]["conserver"]
                 or av["base"]["conserver"],
                 "pourquoi_conserve": purge.CONSERVEES},
        "objets": {"avant": av["objets"], "supprime": etat["fait"]["objets"]["supprime"],
                   "reste": apres["objets"]["versions"]},
        "archives": {"avant": {m: v["tenant"] for m, v in av["archives"].items()},
                     "supprime": etat["fait"]["archives"], "reste": reste_archives},
    }
    for nom, hors in HORS_SCRIPT.items():
        elements[nom] = {"hors_script": hors}
    return {"tenant": etat.get("tenant"), "elements": elements,
            "complet": all(elements[e]["reste"] == 0 for e in ELEMENTS)}


def _constat_texte(c: dict) -> str:
    e = c["elements"]
    lignes = ["", "── CONSTAT ──",
              f"base     : {e['base']['avant']} ligne(s) avant, {e['base']['supprime']} "
              f"supprimée(s), {e['base']['reste']} restante(s) ; conservé : "
              f"{ {t: n for t, n in e['base']['conserve'].items() if n} }",
              f"objets   : {e['objets']['avant']['versions']} version(s) avant, "
              f"{e['objets']['supprime']} supprimée(s), {e['objets']['reste']} restante(s)",
              f"archives : {sum(e['archives']['avant'].values())} ligne(s) du tenant avant, "
              f"{e['archives']['reste']} restante(s)"]
    lignes += [f"{n:<9}: À FAIRE HORS SCRIPT — {v['hors_script']}" for n, v in e.items()
               if "hors_script" in v]
    lignes.append("COMPLET" if c["complet"] else "INCOMPLET : un reste n'est pas nul")
    return "\n".join(lignes)


def base(args) -> int:
    with _connexion(lecture_seule=not args.appliquer) as conn:
        if not args.appliquer:
            print(json.dumps(compte_base(conn, args.tenant, {"orgs": []}), indent=2, default=str))
            print("\nÀ BLANC : rien n'a été écrit.")
            return 0
        journal = purge.vider_par_lots(conn, args.tenant, rapport=_avancement)
        conn.autocommit = False
        plan = purge.appliquer(conn, args.tenant)
        print(json.dumps({"journal_par_lots": journal, "supprime": plan.supprimer,
                          "conserve": plan.conserver}, indent=2))
    return 0


def objets(args) -> int:
    mod = _archivage()
    bucket, s3 = mod._env("OTO_MCP_S3_BUCKET"), mod._s3_client()
    with _connexion(lecture_seule=True) as conn:
        r = releve(conn, args.tenant, args.dossier)
    versions = purge_objets.inventaire(s3, bucket, set(r["cles"]), set(r["prefixes"]))
    print(json.dumps(purge_objets.resume(versions)))
    if args.appliquer:
        purge_objets.supprimer(s3, bucket, versions)
        reste = purge_objets.inventaire(s3, bucket, set(r["cles"]), set(r["prefixes"]))
        print(json.dumps({"reste": purge_objets.resume(reste)}))
        return 0 if not reste else 1
    return 0


def archives(args) -> int:
    mod = _archivage()
    bucket, s3 = mod._env("OTO_MCP_S3_BUCKET"), mod._s3_client()
    with _connexion(lecture_seule=not args.appliquer) as conn, _Verrou(conn, mod):
        r = releve(conn, args.tenant, args.dossier)
        tenant = _tenant_du_journal(r)
        mois = purge_archives.compter(conn, s3, bucket, tenant)
        print(json.dumps(compte_archives(conn, s3, bucket, r), indent=2))
        if args.extraire:
            print(json.dumps(purge_archives.extraire(s3, bucket, tenant,
                                                     [m for m in mois if m.tenant],
                                                     args.extraire)))
        if args.appliquer:
            print(json.dumps(purge_archives.reecrire(conn, s3, bucket, tenant, mois,
                                                     args.extrait), indent=2))
    return 0


def extraire(args) -> int:
    with _connexion(lecture_seule=True) as conn:
        lu = purge.lire(conn, args.tenant)
        if lu is None:
            raise EtapeEchouee(f"tenant {args.tenant!r} absent : rien à remettre")
        os.makedirs(args.dossier, exist_ok=True)
        n = purge_remise.journal(conn, lu, os.path.join(args.dossier, "journal.csv.gz"))
        tard = purge_remise.tardives(conn, lu, args.depuis,
                                     os.path.join(args.dossier, "tardives"))
    print(json.dumps({"journal": n, "tardives": tard}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = p.add_subparsers(dest="commande", required=True)
    for nom in ("tout", "base", "objets", "archives", "extraire"):
        s = sous.add_parser(nom)
        s.add_argument("--tenant", required=True)
        s.add_argument("--dossier", required=nom in ("objets", "extraire"))
        if nom != "extraire":
            s.add_argument("--appliquer", action="store_true")
    sous.choices["tout"].add_argument("--extraction-faite", metavar="DOSSIER")
    g = sous.choices["archives"].add_mutually_exclusive_group()
    g.add_argument("--extraire", metavar="DOSSIER")
    g.add_argument("--extrait", metavar="DOSSIER")
    sous.choices["extraire"].add_argument("--depuis", required=True,
                                          help="instant du transfert (ISO 8601, UTC)")
    args = p.parse_args(argv)
    if args.commande == "archives" and args.appliquer and not args.extrait:
        p.error("archives --appliquer exige --extrait <dossier> (la part déjà remise)")
    try:
        return {"tout": tout, "base": base, "objets": objets, "archives": archives,
                "extraire": extraire}[args.commande](args)
    except (purge.PurgeRefusee, purge.PurgeIncoherente, PerimetreRefuse,
            purge_archives.ArchiveDiscordante, EtapeEchouee) as e:
        print(f"✗ REFUS — {type(e).__name__} : {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
