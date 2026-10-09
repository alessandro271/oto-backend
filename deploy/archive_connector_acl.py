#!/usr/bin/env python3
"""Exporte `connector_acl` au froid et l'atteste, pour que sa révision de retrait parte.

`connector_acl` n'a plus de lecteur ni d'écrivain depuis le 24/09/2026 (ADR 0053 D1),
mais elle porte encore des lignes : qui était autorisé à quel connecteur, dans quelles
orgs. Sa révision de retrait (`0053_connector_acl_retiree`) refuse de la supprimer tant
que ce contenu n'a pas été exporté, relu, et attesté (oto-backend#1169). Ce script est
ce geste, manuel et unique, joué AVANT la révision.

Ce qu'il fait, dans UNE transaction en lecture répétable (le contenu exporté, le compte
et l'empreinte sont ceux d'un même instantané) :
1. constate la table (`to_regclass`) ; absente → rien à faire ; vide → rien à exporter
   (la révision supprime une table vide sans attestation) ; déjà attestée et inchangée
   → rien à refaire, il le dit ;
2. exporte la table entière en CSV (en-tête, ordre de la clé primaire), gzip, vers
   `s3://<OTO_MCP_S3_BUCKET>/archives/connector_acl/<horodatage UTC>.csv.gz`, objet
   privé. ⚠️ Il n'écrase JAMAIS un objet : le bucket n'est pas versionné ;
3. RELIT l'objet depuis l'Object Storage : mêmes octets que ceux déposés, et autant
   d'enregistrements CSV que de lignes en base — sinon il s'arrête sans rien attester ;
4. pose l'attestation (`COMMENT ON TABLE`, `oto_mcp/db/export_avant_retrait.py`) :
   objet, lignes, empreinte, date. C'est elle que la révision exige.

Il n'affiche que des comptes, la clé de l'objet et l'empreinte : aucune ligne, aucun
identifiant de compte, aucun secret.

Usage, sur la box, par le lanceur (mêmes fichiers d'environnement et même rôle de base
que `migrer`, docs/migrations-versionnees.md §5) :

    deploy/lanceur_secrets.py --script deploy/archive_connector_acl.py

Sorties : 0 = exporté et attesté, ou rien à faire ; 1 = refus ou relecture en échec
(rien d'attesté).
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

ICI = Path(__file__).resolve().parent
for chemin in (ICI, ICI.parent):
    if str(chemin) not in sys.path:
        sys.path.insert(0, str(chemin))

import archive_tool_calls as journal  # noqa: E402 — `_env`, `_s3_client`, `_objet_present`

from oto_mcp.db import export_avant_retrait as attestation  # noqa: E402

TABLE = "connector_acl"
CLE_PRIMAIRE = ("scope_type", "scope_id", "connector", "principal_type", "principal_id")
S3_PREFIX = "archives/connector_acl"
STATEMENT_TIMEOUT = "15s"
LOCK_TIMEOUT = "5s"

OK, REFUS = 0, 1


class Refus(RuntimeError):
    """L'export ne peut pas être attesté ; rien ne l'a été."""


def _cle(maintenant: datetime) -> str:
    return f"{S3_PREFIX}/{maintenant.strftime('%Y-%m-%dT%H%M%SZ')}.csv.gz"


def _exporter(conn) -> bytes:
    flux = io.BytesIO()
    with gzip.GzipFile(fileobj=flux, mode="wb", mtime=0) as gz:
        with conn.cursor().copy(
                sql.SQL("COPY (SELECT * FROM {} ORDER BY {}) TO STDOUT "
                        "WITH (FORMAT csv, HEADER true)").format(
                    sql.Identifier(TABLE),
                    sql.SQL(", ").join(map(sql.Identifier, CLE_PRIMAIRE)))) as copy:
            for morceau in copy:
                gz.write(morceau)
    return flux.getvalue()


def _relire(s3, bucket: str, cle: str, depose: bytes, lignes: int) -> None:
    relu = s3.get_object(Bucket=bucket, Key=cle)["Body"].read()
    if relu != depose:
        raise Refus(f"s3://{bucket}/{cle} relu différent de ce qui a été déposé "
                    f"({len(relu)} octets pour {len(depose)}) : rien n'est attesté")
    lecteur = csv.reader(io.StringIO(gzip.decompress(relu).decode("utf-8")))
    entete = next(lecteur, None)
    if not entete:
        raise Refus(f"s3://{bucket}/{cle} vide ou illisible : rien n'est attesté")
    enregistrements = sum(1 for _ in lecteur)
    if enregistrements != lignes:
        raise Refus(f"s3://{bucket}/{cle} : {enregistrements} enregistrements relus pour "
                    f"{lignes} lignes en base : rien n'est attesté")


def archiver(conn, s3, bucket: str, out=print, maintenant: datetime | None = None) -> int:
    """Le geste entier, sur une connexion psycopg neuve, non autocommit, aux lignes en
    tuples (celle de `main`)."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    def scalaire(requete):
        return conn.execute(requete).fetchone()[0]

    try:
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        if scalaire(f"SELECT to_regclass('{TABLE}')") is None:
            out(f"`{TABLE}` absente : déjà retirée, rien à exporter")
            conn.rollback()
            return OK
        lignes, empreinte = attestation.releve(scalaire, TABLE)
        if lignes == 0:
            out(f"`{TABLE}` vide : rien à exporter (la révision la retire sans attestation)")
            conn.rollback()
            return OK
        deja = attestation.lire(scalaire, TABLE)
        if deja and (deja.get("lignes"), deja.get("empreinte")) == (lignes, empreinte):
            out(f"`{TABLE}` déjà exportée et inchangée : {deja.get('objet')} "
                f"({lignes} lignes, empreinte {empreinte}) — rien à refaire")
            conn.rollback()
            return OK
        if deja:
            out(f"attestation précédente périmée ({deja.get('objet')}, {deja.get('lignes')} "
                f"lignes) : la table a changé depuis, nouvel export")

        maintenant = maintenant or datetime.now(timezone.utc)
        cle = _cle(maintenant)
        if journal._objet_present(s3, bucket, cle) is not None:
            raise Refus(f"s3://{bucket}/{cle} existe déjà : une archive n'est jamais "
                        "écrasée. Relancer dans une seconde (la clé est horodatée)")
        depose = _exporter(conn)
        s3.put_object(Bucket=bucket, Key=cle, Body=depose)  # pas d'ACL : objet privé
        _relire(s3, bucket, cle, depose, lignes)
        objet = f"s3://{bucket}/{cle}"
        conn.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(
            sql.Identifier(TABLE),
            sql.Literal(attestation.texte(objet, lignes, empreinte,
                                          maintenant.isoformat(timespec="seconds")))))
        conn.commit()
    except Refus as exc:
        conn.rollback()
        out(f"REFUS : {exc}")
        return REFUS
    out(f"`{TABLE}` exportée : {objet} — {lignes} lignes relues, empreinte {empreinte}, "
        "attestation posée. La révision 0053_connector_acl_retiree peut partir.")
    return OK


def main() -> int:
    bucket = journal._env("OTO_MCP_S3_BUCKET")
    s3 = journal._s3_client()
    with psycopg.connect(journal._env("DATABASE_URL")) as conn:
        return archiver(conn, s3, bucket)


if __name__ == "__main__":
    sys.exit(main())
