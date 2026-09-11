"""Faux transport de l'API admin HelloStock pour les bancs des outils `hellostock_*`.

Il remplace `requests.Session.request` : les outils appellent le VRAI client
d'oto-core (construction d'URL, query string, corps, levée des refus), et c'est
ici que la requête atterrit. Il rejoue le contrat — 401 jeton inconnu, 403 compte
non administrateur, pagination `{items, nextCursor, total}`, filtres refusés en 400,
les trois écritures — et, là où le contrat se tait, ce que fait le serveur réel :
un `PATCH` de demande sur un identifiant inconnu rend une 500, l'envoi n'est jamais
dédoublonné contre les envois passés, `noop` quand la messagerie n'est pas
configurée, 502 quand aucun courriel n'a pu partir. Le banc du transport lui-même
(vrai HTTP) vit dans oto-core, `tests/test_hellostock_client.py`.
"""
from __future__ import annotations

import json as _json
import re
from urllib.parse import urlsplit

ADMIN = "hs_admin"
MEMBRE = "hs_membre"
STATUSES = ("declared", "qualified", "published", "closed")
IDENTITE = re.compile(r"coul[ée]e|acierie", re.I)


class Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.content = _json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._body


def _entreprise(i):
    return {"id": f"e{i}", "name": f"Entreprise {i}", "publicRef": i, "sector": "autre",
            "postalCode": "69000", "city": "Ville", "departement": "69"}


def _contact(i):
    return {"userId": i, "name": f"Membre {i}", "email": f"m{i}@example.test",
            "company": f"Entreprise {i}", "phone": None, "location": None}


def _prov():
    return {"pageVariant": None, "utmSource": "ads", "utmMedium": None, "utmCampaign": None}


class FakeHelloStock:
    def __init__(self):
        self.log: list[tuple[str, str, dict, object]] = []
        self.force: dict[tuple[str, str], tuple[int, object]] = {}
        self.noop = False
        self.demandes = {
            i: {"id": i, "status": "published" if i != 2 else "qualified",
                "source": "form", "createdAt": f"2026-09-0{i}T09:00:00.000Z",
                "matiere": "inox", "nuance": "304L", "format": "tôle",
                "dimensions": "2000x1000", "epaisseur": "3", "quantite": "10",
                "delai": "2026-10-01", "reference": f"REF-{i}", "certificatRequis": True,
                "fichiers": ["a.pdf"], "services": ["decoupe_laser"], "customServices": [],
                "commentaire": "urgent", "contact": _contact(9), "entreprise": _entreprise(9),
                "nbPositionnements": 0, "nbEnvois": 0, "provenance": _prov(),
                "data": {"champ_libre": "x" * 50}}
            for i in range(1, 6)}
        self.envois: list[dict] = []
        self.offres = {
            i: {"id": i, "status": "qualified", "source": "form",
                "createdAt": "2026-09-01T09:00:00.000Z", "matiere": "inox", "nuance": "316L",
                "format": "barre", "dimensions": "ø20", "epaisseur": None, "quantite": "5",
                "etat": "neuf", "prixIndicatif": None, "photos": ["https://s3.test/p.jpg"],
                "commentaire": None, "keywords": [], "departement": "69",
                "certificat": {"dispo": True, "joint": True, "url": f"/api/offres/{i}/certificat",
                               "verdict": "valide", "verdictAt": None, "verdictObsolete": False},
                "contact": _contact(i), "entreprise": _entreprise(i), "nbThreads": 0,
                "provenance": _prov(), "data": None}
            for i in range(1, 4)}
        self.users = {
            i: {"id": i, "name": f"Membre {i}", "email": f"m{i}@example.test", "phone": "0600",
                "isAdmin": i == 1, "createdAt": "2026-01-01T00:00:00.000Z",
                "lastLoginAt": None, "company": f"Entreprise {i}", "sector": "autre",
                "location": "69000 Ville", "companyRole": "owner", "entreprise": _entreprise(i),
                "services": ["decoupe_laser"], "customServices": [], "nbOffres": 1,
                "nbDemandes": 0, "nbPositionnements": 0}
            for i in range(1, 12)}

    # --- routage --------------------------------------------------------------

    def __call__(self, session, method, url, params=None, json=None, **kw):
        path = urlsplit(url).path
        self.log.append((method, path, dict(params or {}), json))
        if (method, path) in self.force:
            return Resp(*self.force[(method, path)])
        auth = session.headers.get("Authorization")
        if auth == f"Bearer {MEMBRE}":
            return Resp(403, {"error": "Forbidden"})
        if auth != f"Bearer {ADMIN}":
            return Resp(401, {"error": "Unauthorized"})
        q = dict(params or {})
        m = re.fullmatch(r"/api/admin/(\w+)(?:/(\d+))?(/envoyer)?", path)
        kind, ident, envoyer = m.groups()
        ident = int(ident) if ident else None
        table = {"demandes": self.demandes, "offres": self.offres, "users": self.users}.get(kind)
        if kind == "positionnements":
            return Resp(200, {"items": [], "nextCursor": None, "total": 0})
        if ident is None:
            return self._page(table, q, desc=kind != "users")
        rec = table.get(ident)
        if method == "GET":
            if rec is None:
                return Resp(404, {"error": "Introuvable"})
            out = dict(rec)
            if kind == "demandes":
                out.update(positionnements=[], envois=[
                    e for e in reversed(self.envois) if e["demandeId"] == ident])
            return Resp(200, out)
        if method == "PATCH":
            return self._patch(kind, rec, json or {})
        if envoyer:
            return self._envoyer(rec, json or {})
        return Resp(404, {"error": "Route inconnue"})

    def _page(self, table, q, desc):
        limit = int(q.get("limit", 50))
        if not 1 <= limit <= 200:
            return Resp(400, {"error": "limit doit être un entier entre 1 et 200"})
        if "status" in q and q["status"] not in STATUSES:
            return Resp(400, {"error": f"status inconnu : {q['status']}"})
        rows = sorted(table.values(), key=lambda r: -r["id"] if desc else r["id"])
        if "cursor" in q:
            ids = [str(r["id"]) for r in rows]
            if q["cursor"] not in ids:
                return Resp(400, {"error": "cursor invalide"})
            rows = rows[ids.index(q["cursor"]) + 1:]
        page = rows[:limit]
        return Resp(200, {"items": page, "total": len(table),
                          "nextCursor": str(page[-1]["id"]) if len(rows) > limit else None})

    def _patch(self, kind, rec, body):
        if kind == "demandes":
            if body.get("status") not in STATUSES:
                return Resp(400, {"error": "Statut invalide"})
            if rec is None:
                return Resp(500, {"error": "Internal Server Error"})
            rec["status"] = body["status"]
            return Resp(200, {"success": True})
        bad = [k for k in body.get("keywords") or [] if IDENTITE.search(k)]
        if bad:
            return Resp(400, {"error": f"anonymat : « {bad[0]} » désigne un n° de coulée "
                                       "ou de commande (« coulée ») — identifiant privé"})
        if rec is None:
            return Resp(404, {"error": "Offre introuvable"})
        if "keywords" in body:
            rec["keywords"] = sorted({" ".join(k.split()).lower() for k in body["keywords"]})
        if "status" in body:
            rec["status"] = body["status"]
        return Resp(200, {"success": True})

    def _envoyer(self, rec, body):
        if rec is None:
            return Resp(404, {"error": "Demande introuvable"})
        ids = list(dict.fromkeys(body.get("userIds") or []))
        inconnus = [u for u in ids if u not in self.users]
        if inconnus:
            return Resp(400, {"error": "Destinataires inconnus : " + ", ".join(map(str, inconnus))})
        for u in ids:
            self.envois.append({"id": f"env{len(self.envois)}", "demandeId": rec["id"],
                                "userId": u, "sentById": 1, "message": body.get("message"),
                                "sentAt": "2026-09-11T10:00:00.000Z",
                                "user": {"id": u, "name": f"Membre {u}",
                                         "email": f"m{u}@example.test", "company": None},
                                "sentBy": {"id": 1, "name": "Membre 1",
                                           "email": "m1@example.test"}})
        return Resp(200, {"success": True, "envoyes": len(ids), "echecs": [],
                          "noop": self.noop})

    def ecritures(self):
        return [(m, p) for m, p, _, _ in self.log if m != "GET"]
