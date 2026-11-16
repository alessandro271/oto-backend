"""Purger un tenant tiers parti sur sa propre instance : base, journal, archives, objets.

Le plan d'exécution est `docs/purge-de-tenant.md` ; chaque geste est décrit dans son
module (`oto_mcp/export_perimetre/purge*.py`).

    purger_tenant.py tout --tenant <slug> --dossier D           # à blanc ; FIGE le périmètre
    purger_tenant.py tout --tenant <slug> --dossier D --appliquer [--extraction-faite E]
    purger_tenant.py base --tenant <slug>                         # comptes seuls
    purger_tenant.py objets --tenant <slug>                       # comptes seuls
    purger_tenant.py archives --tenant <slug>                     # comptes seuls, par mois
    purger_tenant.py extraire --tenant <slug> --dossier R --depuis <instant>
        # la remise au titulaire, sur décision : elle lit du CONTENU (jamais un secret)

Le passage (`tout --appliquer`) exige le compte à blanc fait sur le MÊME dossier : il y a
figé le périmètre (`etat.json`), comparé ensuite à une lecture fraîche tant que le tenant
existe — le moindre écart refuse. Ordre : la base (une transaction, tout sauf le journal),
le journal par lots, les archives (sous le verrou de l'archivage quotidien), les objets,
puis le recompte depuis le périmètre figé et le constat (`constat.json`). Une étape faite
n'est pas rejouée ; une étape qui échoue s'inscrit dans `etat.json` et tout s'arrête ;
rejoué sur le même dossier, le passage reprend là.

Les comptes tournent dans une transaction `READ ONLY` explicite. L'environnement est
celui du backend (`DATABASE_URL`, `OTO_MCP_S3_*`) ; sur la box, par le lanceur
(`deploy/lanceur_secrets.py --script scripts/purger_tenant.py …`). Un refus s'imprime
nommé et sort en code 2.
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

ETAPES = ("base", "journal", "archives", "objets")    # l'ordre du passage
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


def _connexion() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


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


def _public_base() -> str:
    from oto_mcp import media_store
    return media_store.public_base()


# ── l'état d'un passage ──────────────────────────────────────────────────────────────

def _lire_json(dossier: str, nom: str) -> dict | None:
    chemin = os.path.join(dossier, nom)
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def _ecrire_json(dossier: str, nom: str, donnees: dict) -> None:
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, nom)
    with open(chemin + ".tmp", "w", encoding="utf-8") as f:
        json.dump(donnees, f, indent=2, ensure_ascii=False, default=str)
    os.replace(chemin + ".tmp", chemin)


def _tenant_du_journal(fige) -> purge_archives.Tenant:
    return purge_archives.Tenant(*purge.classeur_du_journal(fige))


# ── les comptes (lecture seule) ──────────────────────────────────────────────────────

def compte_base(conn, slug: str) -> dict:
    with purge.en_lecture_seule(conn):
        plan = purge.a_blanc(conn, slug)
        par_mois = purge.journal_par_mois(conn, slug)
    return {"present": plan.id_tenant is not None, "orgs": len(plan.orgs),
            "comptes": plan.comptes, "supprimer": plan.supprimer,
            "total": sum(plan.supprimer.values()), "dont_appels_de_support":
            plan.appels_de_support, "journal_par_mois": par_mois, "conserver": plan.conserver}


def releve(conn, slug: str) -> dict:
    """Le périmètre frais et ses objets, à figer."""
    with purge.en_lecture_seule(conn):
        lu = purge.lire(conn, slug)
        if lu is None:
            raise EtapeEchouee(f"tenant {slug!r} absent : rien à relever")
        prefixes, hors = purge.objets(conn, lu, _public_base())
    return {"tenant": slug, "perimetre": purge.en_dict(lu.perimetre),
            "prefixes": sorted(prefixes), "cles_hors_prefixe": len(hors)}


def compte_objets(s3, bucket: str, r: dict) -> dict:
    return {**purge_objets.resume(purge_objets.inventaire(s3, bucket, set(r["prefixes"]))),
            "cles_hors_prefixe_non_touchees": r["cles_hors_prefixe"]}


def compte_archives(conn, s3, bucket: str, fige, extrait: str | None = None) -> dict:
    with purge.en_lecture_seule(conn):
        mois = purge_archives.compter(conn, s3, bucket, _tenant_du_journal(fige), extrait)
    return {m.mois: {"cle": m.cle, "total": m.total, "tenant": m.tenant, "reste": m.reste,
                     "versions_anciennes": m.versions_anciennes,
                     "registre_en_retard": m.registre_en_retard} for m in mois}


# ── tout ─────────────────────────────────────────────────────────────────────────────

def tout(args) -> int:
    mod = _archivage()
    bucket, s3 = mod._env("OTO_MCP_S3_BUCKET"), mod._s3_client()
    with _connexion() as conn:
        return (_passage if args.appliquer else _a_blanc)(args, conn, mod, bucket, s3)


def _a_blanc(args, conn, _mod, bucket, s3) -> int:
    r = releve(conn, args.tenant)
    deja = _lire_json(args.dossier, "etat.json")
    if deja is not None:
        fige = purge.de_dict(deja["perimetre"])
        with purge.en_lecture_seule(conn):
            purge.verifier_fige(conn, fige)
    else:
        _ecrire_json(args.dossier, "etat.json", {**r, "fait": {}})
        fige = purge.de_dict(r["perimetre"])
    versionnement = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    avant = {"base": compte_base(conn, args.tenant), "objets": compte_objets(s3, bucket, r),
             "archives": compte_archives(conn, s3, bucket, fige),
             "seau_versionne": versionnement == "Enabled"}
    print(json.dumps({"a_blanc": avant, "hors_script": HORS_SCRIPT}, indent=2,
                     ensure_ascii=False, default=str))
    print(f"\nÀ BLANC : rien n'a été écrit en base ni sur le stockage. Périmètre figé dans "
          f"{os.path.join(args.dossier, 'etat.json')}.")
    return 0


def _passage(args, conn, mod, bucket, s3) -> int:
    etat = _lire_json(args.dossier, "etat.json")
    if etat is None or etat.get("tenant") != args.tenant:
        raise EtapeEchouee(f"aucun périmètre figé pour {args.tenant!r} dans {args.dossier} : "
                           "faire d'abord le compte à blanc sur ce dossier")
    fige = purge.de_dict(etat["perimetre"])
    if "base" not in etat["fait"]:
        with purge.en_lecture_seule(conn):
            purge.verifier_fige(conn, fige)
        etat.setdefault("avant", {"base": compte_base(conn, args.tenant)})
        _ecrire_json(args.dossier, "etat.json", etat)
    extrait = args.extraction_faite or os.path.join(args.dossier, "journal-archive")
    tenant = _tenant_du_journal(fige)
    etape = None
    try:
        for etape in ETAPES:
            if etape in etat["fait"]:
                continue
            if etape == "base":
                plan = purge.appliquer(conn, fige)
                fait = {"supprime": sum(plan.supprimer.values()), "par_table": plan.supprimer}
            elif etape == "journal":
                fait = {"supprime": purge.vider_journal(conn, fige, rapport=_avancement)}
            elif etape == "archives":
                with _Verrou(conn, mod):
                    purge_archives.exiger_versionnement(s3, bucket)
                    mois = purge_archives.compter(conn, s3, bucket, tenant, extrait)
                    etat.setdefault("archives_avant", {m.mois: m.tenant for m in mois})
                    purge_archives.extraire(s3, bucket, tenant, [m for m in mois if m.tenant],
                                            extrait)
                    fait = {"extrait": extrait, "mois": purge_archives.reecrire(
                        conn, s3, bucket, tenant, mois, extrait)}
            else:
                versions = purge_objets.inventaire(s3, bucket, set(etat["prefixes"]))
                etat.setdefault("objets_avant", purge_objets.resume(versions))
                fait = {"supprime": purge_objets.supprimer(s3, bucket, versions)}
            etat["fait"][etape] = fait
            etat.pop("echec", None)
            _ecrire_json(args.dossier, "etat.json", etat)
    except Exception as e:
        etat["echec"] = {"etape": etape, "erreur": f"{type(e).__name__} : {e}",
                         "faites": list(etat["fait"])}
        _ecrire_json(args.dossier, "etat.json", etat)
        print(json.dumps(etat["echec"], indent=2, ensure_ascii=False), file=sys.stderr)
        raise
    with purge.en_lecture_seule(conn):
        recompte = purge.recompter(conn, fige)
    apres = {"base": recompte, "objets": compte_objets(s3, bucket, etat),
             "archives": compte_archives(conn, s3, bucket, fige, extrait)}
    constat = _constat(etat, apres)
    _ecrire_json(args.dossier, "constat.json", constat)
    print(json.dumps(constat, indent=2, ensure_ascii=False, default=str))
    print(_constat_texte(constat))
    return 0 if constat["complet"] else 1


def _avancement(fait: int, total: int) -> None:
    print(f"  journal : ids {min(fait, total)}/{total}", file=sys.stderr, flush=True)


def _constat(etat: dict, apres: dict) -> dict:
    av, fait, rb = etat["avant"]["base"], etat["fait"], apres["base"]
    arch = apres["archives"]
    elements = {
        "base": {"avant": av["total"],
                 "dont_appels_de_support": av["dont_appels_de_support"],
                 "supprime": fait["base"]["supprime"] + fait["journal"]["supprime"],
                 "reste": sum(rb.supprimer.values()),
                 "reste_par_table": {t: n for t, n in rb.supprimer.items() if n},
                 "conserve": {t: n for t, n in rb.conserver.items() if n},
                 "pourquoi_conserve": purge.CONSERVEES},
        "objets": {"avant": etat.get("objets_avant"), "supprime": fait["objets"]["supprime"],
                   "reste": apres["objets"]["versions"],
                   "cles_hors_prefixe_non_touchees":
                       apres["objets"]["cles_hors_prefixe_non_touchees"]},
        "archives": {"avant": etat.get("archives_avant"), "fait": fait["archives"]["mois"],
                     "reste": sum(m["tenant"] for m in arch.values())
                     + sum(1 for m in arch.values() if m["registre_en_retard"])},
    }
    for nom, hors in HORS_SCRIPT.items():
        elements[nom] = {"hors_script": hors}
    return {"tenant": etat["tenant"], "elements": elements,
            "complet": all(elements[e]["reste"] == 0 for e in ("base", "objets", "archives"))}


def _constat_texte(c: dict) -> str:
    e = c["elements"]
    b, o, a = e["base"], e["objets"], e["archives"]
    lignes = ["", "── CONSTAT ──",
              f"base     : {b['avant']} ligne(s) avant (dont {b['dont_appels_de_support']} "
              f"appels de support), {b['supprime']} supprimée(s), {b['reste']} restante(s) ; "
              f"conservé : {b['conserve']}",
              f"objets   : {(o['avant'] or {}).get('versions', 0)} version(s) avant, "
              f"{o['supprime']} supprimée(s), {o['reste']} restante(s) ; "
              f"{o['cles_hors_prefixe_non_touchees']} clé(s) hors préfixe non touchée(s)",
              f"archives : {sum((a['avant'] or {}).values())} ligne(s) du tenant avant, "
              f"{a['reste']} restante(s)"]
    lignes += [f"{n:<9}: À FAIRE HORS SCRIPT — {v['hors_script']}" for n, v in e.items()
               if "hors_script" in v]
    lignes.append("COMPLET" if c["complet"] else "INCOMPLET : un reste n'est pas nul")
    return "\n".join(lignes)


# ── comptes seuls ────────────────────────────────────────────────────────────────────

def base(args) -> int:
    with _connexion() as conn:
        print(json.dumps(compte_base(conn, args.tenant), indent=2, default=str))
    return 0


def objets(args) -> int:
    mod = _archivage()
    with _connexion() as conn:
        r = releve(conn, args.tenant)
    print(json.dumps(compte_objets(mod._s3_client(), mod._env("OTO_MCP_S3_BUCKET"), r),
                     indent=2))
    return 0


def archives(args) -> int:
    mod = _archivage()
    with _connexion() as conn:
        r = releve(conn, args.tenant)
        print(json.dumps(compte_archives(conn, mod._s3_client(), mod._env("OTO_MCP_S3_BUCKET"),
                                         purge.de_dict(r["perimetre"])), indent=2))
    return 0


def extraire(args) -> int:
    with _connexion() as conn, purge.en_lecture_seule(conn):
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
        if nom in ("tout", "extraire"):
            s.add_argument("--dossier", required=True)
    sous.choices["tout"].add_argument("--appliquer", action="store_true")
    sous.choices["tout"].add_argument("--extraction-faite", metavar="DOSSIER")
    sous.choices["extraire"].add_argument("--depuis", required=True,
                                          help="instant du transfert (ISO 8601, UTC)")
    args = p.parse_args(argv)
    try:
        return {"tout": tout, "base": base, "objets": objets, "archives": archives,
                "extraire": extraire}[args.commande](args)
    except (purge.PurgeRefusee, purge.PurgeIncoherente, PerimetreRefuse,
            purge_archives.ArchiveDiscordante, EtapeEchouee) as e:
        print(f"✗ REFUS — {type(e).__name__} : {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
