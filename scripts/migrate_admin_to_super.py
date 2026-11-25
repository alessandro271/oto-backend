"""One-shot migration : `admin` → `super_admin` (3e palier de rôle plateforme).

Contexte : on a introduit un palier intermédiaire `admin` (OPÉRATEUR : supervision
plateforme — users, monitoring, activation des connecteurs, maintenance — SANS
escalade en masse vers les orgs tierces) **sous** le tout-puissant, désormais
nommé `super_admin`.

Les `admin` existants en base étaient les tout-puissants d'avant (escalade
org_admin de toutes les orgs, platform keys, tokens, rôles, orgs tierces). Pour
préserver leurs droits, on les reclasse en `super_admin`. Un `admin` post-migration
sera donc un opérateur au sens nouveau, pas un tout-puissant.

À lancer **sur la box, APRÈS déploiement du code** (sinon un sub `admin` resterait
sans le nouveau palier le temps du déploiement) :

    ssh -i ~/.ssh/<clé> root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.migrate_admin_to_super"

Idempotent : ne touche que les lignes `role='admin'` ; relancé, il n'en trouve plus
(les bootstrap `OTO_MCP_ADMIN_SUB` sont forcés super_admin par le code, hors DB —
ne sont pas concernés). `--dry-run` pour compter sans écrire.

Nommer `super_admin` est une nomination comme une autre : sous `OTO_ADMIN_MFA`
`report` ou `enforce`, chaque compte est jugé par `garde_admin.refus_promotion` (second
facteur fort enrôlé chez Logto). En `enforce`, un seul refus et RIEN n'est écrit : le
script nomme les comptes à équiper et sort en erreur. En `report`, le journal les nomme
et l'écriture se fait.
"""
from __future__ import annotations

import os
import sys

import psycopg

from oto_mcp import garde_admin


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Liste avant écriture (pour le compte-rendu, et seul lookup en dry-run).
            cur.execute("SELECT sub, email FROM users WHERE role = 'admin' ORDER BY created_at")
            rows = cur.fetchall()
            if not rows:
                print("Aucun user role='admin' — rien à migrer (déjà à jour ?).")
                return
            print(f"{len(rows)} user(s) admin → super_admin :")
            for sub, email in rows:
                print(f"  - {sub}  {email or '(sans email)'}")
            refus = [(sub, m) for sub, _ in rows
                     if (m := garde_admin.refus_promotion(sub, "super_admin"))]
            for sub, m in refus:
                print(f"  ✗ {sub} : {m.code} — {m.message}")
            if dry_run:
                print("\n[dry-run] aucune écriture.")
                return
            if refus:
                sys.exit(f"{len(refus)} compte(s) sans second facteur fort : aucune "
                         "écriture (OTO_ADMIN_MFA=enforce).")
            cur.execute("UPDATE users SET role = 'super_admin', updated_at = NOW() "
                        "WHERE role = 'admin'")
            print(f"\n{cur.rowcount} ligne(s) mises à jour.")
        conn.commit()


if __name__ == "__main__":
    main()
