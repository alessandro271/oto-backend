"""Le banc partagé des tests d'export et d'import de périmètre (oto-backend#1088).

Un propriétaire = un TENANT tiers, une org, deux comptes qualifiés par le tenant
(`<slug>:<m>-alice`), un espace perso, une équipe, et une ligne de chaque famille de
contenu. Chaque ligne porte le MARQUEUR `m` du propriétaire dans une valeur texte :
c'est ce qui permet de vérifier « rien d'autrui, rien d'oublié » par un second chemin,
sans passer par les règles du classement.
"""
from __future__ import annotations

import json

from oto_mcp import credentials_store, runner_hook, transcription_worker
from oto_mcp.crypto import encrypt_with_key

A, B = "A7d1e", "B9f3c"
SECRET = "clair-{}-{}"


def slug_de(m: str) -> str:
    return f"t{m.lower()}"


def tenant(c, slug: str, nom: str) -> int:
    return c.execute("INSERT INTO tenants (slug, name) VALUES (%s, %s) RETURNING id",
                     (slug, nom)).fetchone()["id"]


def org(c, nom: str, tenant_id: int, personal_of: str | None = None) -> int:
    return c.execute("INSERT INTO orgs (name, personal_of, tenant_id) VALUES (%s, %s, %s) "
                     "RETURNING id", (nom, personal_of, tenant_id)).fetchone()["id"]


def membre(c, org_id: int, sub: str) -> None:
    c.execute("INSERT INTO users (sub, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
              (sub, f"{sub.replace(':', '.')}@exemple.test"))
    c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'admin')",
              (org_id, sub))


def _credential(c, cle: bytes, entity_type: str, entity_id: str, connector: str,
                m: str) -> None:
    aad = credentials_store._aad(entity_type, entity_id, connector, "")
    c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
              "account, secret_enc) VALUES (%s, %s, %s, '', %s)",
              (entity_type, entity_id, connector,
               encrypt_with_key(cle, SECRET.format(m, connector), aad)))


def semer(c, m: str, *, cle: bytes | None = None) -> dict:
    """Sème le propriétaire `m` ; avec `cle`, ses trois sortes de secrets en plus."""
    slug = slug_de(m)
    tid = tenant(c, slug, f"tenant {m}")
    o = org(c, f"org {m}", tid)
    alice, bob = f"{slug}:{m}-alice", f"{slug}:{m}-bob"
    membre(c, o, alice)
    membre(c, o, bob)
    membre(c, org(c, f"perso {m}", tid, personal_of=alice), alice)
    c.execute("INSERT INTO tenant_admins (slug, sub, granted_by) VALUES (%s, %s, %s)",
              (slug, alice, f"admin {m}"))
    c.execute("INSERT INTO tenant_legal_docs (tenant_slug, doc_slug, version, label, url) "
              "VALUES (%s, 'cgu', '1', %s, 'https://exemple.test/cgu')", (slug, f"CGU {m}"))
    c.execute("INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, "
              "set_by) VALUES ('tenant', %s, 'serper', false, %s)", (slug, f"coupure {m}"))
    groupe = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
                       (o, f"équipe {m}")).fetchone()["id"]
    c.execute("INSERT INTO org_group_members (group_id, sub, group_role) "
              "VALUES (%s, %s, 'member')", (groupe, bob))
    projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                       "VALUES ('org', %s, %s) RETURNING id", (str(o), f"projet {m}")
                       ).fetchone()["id"]
    c.execute("INSERT INTO projects (owner_type, owner_id, name) VALUES ('user', %s, %s)",
              (alice, f"projet perso {m}"))
    page = c.execute("INSERT INTO docs (project_id, title, body_md) VALUES (%s, %s, %s) "
                     "RETURNING id", (projet, f"page {m}", f"corps {m}")).fetchone()["id"]
    c.execute("INSERT INTO docs (project_id, parent_id, title, body_md) "
              "VALUES (%s, %s, %s, '')", (projet, page, f"sous-page {m}"))
    c.execute("INSERT INTO doc_revisions (doc_id, title, body_md) VALUES (%s, %s, %s)",
              (page, f"page {m}", f"v1 {m}"))
    c.execute("INSERT INTO project_files (project_id, s3_key, filename, mime, size_bytes) "
              "VALUES (%s, %s, %s, 'text/plain', 3)", (projet, f"projets/{m}/f.txt", f"f {m}"))
    c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
              "principal_id, granted_by) VALUES ('project', %s, 'group', %s, %s)",
              (str(projet), str(groupe), alice))
    tableau = c.execute("INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                        "VALUES ('org', %s, %s) RETURNING id", (str(o), f"tableau_{m}")
                        ).fetchone()["id"]
    for i in (1, 2):
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                  (tableau, f"{m}-{i}", json.dumps({"nom": f"ligne {m}", "par": bob})))
    noeud = c.execute("INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
                      "VALUES (%s, 'page', 'org', %s, %s) RETURNING id",
                      (f"n-{m}", str(o), json.dumps({"titre": m}))).fetchone()["id"]
    c.execute("INSERT INTO blocks (public_id, node_id, position, type, props) "
              "VALUES (%s, %s, 1, 'paragraph', %s)", (f"b-{m}", noeud, json.dumps({"t": m})))
    c.execute("INSERT INTO org_instructions (org_id, owner_type, owner_id, slug, body_md) "
              "VALUES (%s, 'org', %s, %s, %s)", (o, str(o), f"proc-{m}", f"fais {m}"))
    c.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES (%s, %s, %s, %s)",
              (f"run-{m}", alice, o, f"run {m}"))
    c.execute("INSERT INTO run_messages (run_id, seq, role, content) "
              "VALUES (%s, 1, 'user', %s)", (f"run-{m}", json.dumps({"texte": m})))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id, args) "
              "VALUES ('oto', 'tool', %s, 'oto_doc', %s, %s)",
              (alice, o, json.dumps({"_m": m, "pour": alice})))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool) "
              "VALUES ('oto', 'tool', %s, 'oto_whoami')", (bob,))
    c.execute("INSERT INTO usage (sub, tool, day, count) VALUES (%s, 'oto_doc', "
              "CURRENT_DATE, 3)", (bob,))
    c.execute("INSERT INTO billing_contracts (org_id, seats, unit_amount, reference, "
              "starts_at) VALUES (%s, 1, 100, %s, NOW())", (o, f"contrat {m}"))
    c.execute("INSERT INTO org_entitlements (sub, right_key, value, source) "
              "VALUES (%s, 'essai', 1, %s)", (alice, f"commerce {m}"))
    if cle is not None:
        _credential(c, cle, "org", str(o), "serper", m)
        _credential(c, cle, "member", f"{o}:{alice}", "apollo", m)
        _credential(c, cle, "user", alice, "tavily", m)
        _credential(c, cle, "tenant", slug, "pappers", m)
        declencheur = c.execute(
            "INSERT INTO runner_triggers (org_id, sub, label, procedure, kind, tools) "
            "VALUES (%s, %s, %s, 'p', 'webhook', '[]') RETURNING id", (o, alice, f"hook {m}")
        ).fetchone()["id"]
        c.execute("UPDATE runner_triggers SET hook_signing_secret_enc = %s WHERE id = %s",
                  (encrypt_with_key(cle, SECRET.format(m, "hook"),
                                    runner_hook._aad_du_secret(declencheur)), declencheur))
        audio = f"audio/{m}/a.mp3"
        c.execute("INSERT INTO transcription_jobs (project_id, sub, status, audio_key, "
                  "filename, mime, api_key_enc) VALUES (%s, %s, 'queued', %s, %s, "
                  "'audio/mpeg', %s)",
                  (projet, alice, audio, f"a {m}.mp3",
                   encrypt_with_key(cle, SECRET.format(m, "transcription"),
                                    transcription_worker._aad(audio))))
    return {"org": o, "tenant": tid, "slug": slug, "projet": projet, "page": page,
            "alice": alice, "bob": bob}
