"""Le palmarès des refus des limiteurs (#1232, lot 1) : `oto_mcp/limite_refus_top.py`.

Ce qu'on éprouve, sans base : QUI serait coupé est compté par (limiteur, classe, clé
HACHÉE), en mémoire, et ne part qu'à la minute close ; jamais un jeton, une adresse ni
un porteur en clair ; borné en clés ; et l'écriture ne bloque ni ne casse jamais
l'appelant. Adresses des plages de documentation (RFC 5737)."""
from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from oto_mcp import limite_debit as ld
from oto_mcp import limite_refus_top as top

A, B = "192.0.2.10", "198.51.100.20"
T0 = 1_760_000_000.0      # 2025-10-09 08:53:20 UTC : une minute murale quelconque


def test_le_palmares_cumule_par_cle_et_ne_rend_qu_a_la_minute_close():
    p = top.Palmares(top.DEBIT)
    assert p.noter("anonyme", "k1", "report", T0) == []
    assert p.noter("anonyme", "k1", "report", T0 + 1) == []
    assert p.noter("jeton", "k2", "report", T0 + 2) == []
    assert p.basculer(T0 + 3) == [], "même minute : rien ne part"

    lignes = p.basculer(T0 + 60)
    par_cle = {(l["classe"], l["cle"]): l for l in lignes}
    assert set(par_cle) == {("anonyme", "k1"), ("jeton", "k2")}
    k1 = par_cle[("anonyme", "k1")]
    assert (k1["limiteur"], k1["refus"], k1["mode"]) == ("debit", 2, "report")
    assert k1["jour"] == date(2025, 10, 9)
    assert k1["premier_at"] < k1["dernier_at"]
    assert p.basculer(T0 + 120) == [], "une minute sans refus n'écrit rien"


def test_le_jour_est_celui_de_la_minute_close_pas_celui_de_l_ecriture():
    minuit = 1_760_054_400.0                       # 2025-10-10 00:00:00 UTC
    p = top.Palmares(top.PLATEFORME)
    p.noter("serper", "k", "report", minuit - 30)
    (ligne,) = p.noter("serper", "k", "report", minuit + 5)
    assert ligne["jour"] == date(2025, 10, 9)


def test_au_dela_de_la_borne_les_refus_vont_au_debordement():
    p = top.Palmares(top.DEBIT, max_cles=2)
    for cle in ("a", "b", "c", "d", "c"):
        p.noter("anonyme", cle, "report", T0)
    lignes = {l["cle"]: l["refus"] for l in p.basculer(T0 + 60)}
    assert lignes == {"a": 1, "b": 1, top.DEBORDEMENT: 3}, "le total reste juste"


def test_l_environnement_separe_preproduction_et_production(monkeypatch):
    monkeypatch.setenv("OTO_SENTRY_ENV", "canari")
    p = top.Palmares(top.DEBIT)
    p.noter("anonyme", "k", "report", T0)
    assert p.basculer(T0 + 60)[0]["env"] == "canari"


def test_l_empreinte_est_celle_du_journal_du_debit():
    assert top.empreinte("Bearer x") == ld._empreinte(b"Bearer x")
    assert len(top.empreinte("org:42")) == 32


# ── la limite de débit applicative ────────────────────────────────────────────

def _app(scope, receive, send):
    async def go():
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return go()


def _scope(chemin="/mcp", ip=A, jeton=None):
    h = [(b"cf-connecting-ip", ip.encode())]
    if jeton is not None:
        h.append((b"authorization", f"Bearer {jeton}".encode()))
    return {"type": "http", "method": "POST", "path": chemin, "headers": h,
            "client": ("127.0.0.1", 5000)}


async def _appel(garde, scope):
    async def receive():                                   # pragma: no cover
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_m):
        return None

    await garde(scope, receive, send)


def _garde(refus, murale, mode=ld.REPORT, **par_minute):
    bornes = {c: ld.Borne(par_minute.get(c, d)) for c, d in ld.DEFAUTS.items()}
    return ld.LimiteDebit(_app, mode=mode, bornes=bornes, exemptees=(),
                          horloge=lambda: 1000.0, horloge_murale=lambda: murale["t"],
                          publier=lambda lignes: None, publier_refus=refus.extend)


@pytest.mark.asyncio
async def test_le_debit_note_qui_serait_refuse_par_cle_hachee():
    refus, murale = [], {"t": T0}
    garde = _garde(refus, murale, anonyme=4, jeton=4,
                   webhook=2)
    for _ in range(5):
        await _appel(garde, _scope(ip=A))                 # rafale 2 : 3 « refusés »
    for _ in range(4):
        await _appel(garde, _scope(jeton="secret-tres-long"))   # rafale 2 : 2
    for _ in range(3):
        await _appel(garde, _scope(chemin="/api/hooks/s3cr3t-crochet", ip=B))  # 2
    assert refus == [], "rien n'est écrit tant que la minute n'est pas close"

    murale["t"] += 60
    await _appel(garde, _scope(ip=B, chemin="/api/version"))
    par_classe = {l["classe"]: l for l in refus}
    assert {c: l["refus"] for c, l in par_classe.items()} == {
        "anonyme": 3, "jeton": 2, "webhook": 2}
    assert all(l["limiteur"] == "debit" and l["mode"] == "report" for l in refus)
    assert par_classe["anonyme"]["cle"] == ld._empreinte(A.encode())
    assert par_classe["jeton"]["cle"] == ld._empreinte(b"Bearer secret-tres-long")
    assert par_classe["webhook"]["cle"] == ld._empreinte(
        f"{B} /api/hooks/s3cr3t-crochet".encode())
    brut = json.dumps(refus, default=str)
    for secret in (A, B, "secret-tres-long", "s3cr3t-crochet"):
        assert secret not in brut, "jamais une adresse, un jeton ni un chemin en clair"


@pytest.mark.asyncio
async def test_en_enforce_le_palmares_compte_les_refus_servis():
    refus, murale = [], {"t": T0}
    garde = _garde(refus, murale, mode=ld.ENFORCE, anonyme=2)
    for _ in range(3):
        await _appel(garde, _scope())
    murale["t"] += 60
    await _appel(garde, _scope(ip=B))
    (ligne,) = refus
    assert (ligne["classe"], ligne["refus"], ligne["mode"]) == ("anonyme", 2, "enforce")


@pytest.mark.asyncio
async def test_sans_refus_le_debit_n_ecrit_rien():
    refus, murale = [], {"t": T0}
    garde = _garde(refus, murale)
    for i in range(50):
        await _appel(garde, _scope(ip=f"192.0.2.{i}"))
    murale["t"] += 60
    await _appel(garde, _scope())
    assert refus == []


# ── la clé plateforme ─────────────────────────────────────────────────────────

@pytest.fixture
def plateforme(monkeypatch):
    """Un palmarès neuf à la place du singleton du processus, publications captées."""
    from oto_mcp.access import platform_rate
    palmares, publiees = top.Palmares(top.PLATEFORME), []
    monkeypatch.setattr(top, "PALMARES_PLATEFORME", palmares)
    monkeypatch.setattr(top, "publier", publiees.extend)
    monkeypatch.setattr(platform_rate.providers, "credential_provider", lambda p: p)
    return palmares, publiees


@pytest.mark.parametrize("mode", ["report", "enforce"])
def test_la_cle_plateforme_note_le_porteur_hache_au_dela_de_la_limite(
        monkeypatch, plateforme, mode):
    from oto_mcp.access import platform_rate
    palmares, publiees = plateforme
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", mode)
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE_LIMIT", "2")
    compte = iter(range(1, 10))
    monkeypatch.setattr(platform_rate.db_rate, "compter", lambda *a: next(compte))
    for _ in range(4):
        try:
            platform_rate.enforce("serper", "sub-x", 42)
        except platform_rate.McpError:
            assert mode == "enforce"
    lignes = palmares.basculer(10 ** 10)
    assert [(l["limiteur"], l["classe"], l["cle"], l["refus"], l["mode"])
            for l in lignes] == [("platform_rate", "serper", top.empreinte("org:42"), 2,
                                  mode)]
    assert "42" not in json.dumps([l["cle"] for l in lignes])


def test_sous_la_limite_la_cle_plateforme_ne_note_rien(monkeypatch, plateforme):
    from oto_mcp.access import platform_rate
    palmares, _ = plateforme
    monkeypatch.setenv("OTO_PLATFORM_KEY_ORG_RATE", "report")
    monkeypatch.setattr(platform_rate.db_rate, "compter", lambda *a: 1)
    platform_rate.enforce("serper", "sub-x", None)
    assert palmares.basculer(10 ** 10) == []


# ── l'écriture : hors de l'appelant, jamais un échec qui remonte ──────────────

def test_publier_ecrit_sur_le_fil_d_ecriture_puis_purge_une_fois_par_jour(monkeypatch):
    import threading
    from oto_mcp.db import limite_refus_jour as db_refus
    ecrites, purges, fils = [], [], []

    def ajouter(lignes):
        fils.append(threading.current_thread().name)
        ecrites.append(lignes)

    monkeypatch.setattr(db_refus, "ajouter", ajouter)
    monkeypatch.setattr(db_refus, "purger", purges.append)
    monkeypatch.setattr(top, "_DERNIERE_PURGE", None)
    top.publier([])
    top.publier([{"cle": "k"}])
    top.publier([{"cle": "k2"}])
    top._FILE.join()
    assert ecrites == [[{"cle": "k"}], [{"cle": "k2"}]]
    assert purges == [top.RETENTION_JOURS], "une purge par jour et par processus"
    assert fils == ["oto-refus-top", "oto-refus-top"]
    assert top._FIL.daemon, "un arrêt n'attend jamais la base"


def test_file_pleine_la_minute_est_perdue_sans_bloquer(monkeypatch, caplog):
    import queue
    monkeypatch.setattr(top, "_FILE", queue.Queue(maxsize=1))
    monkeypatch.setattr(top, "_demarrer_fil", lambda: None)   # personne ne vide
    top.publier([{"cle": "a"}])
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_refus_top"):
        top.publier([{"cle": "b"}])
    assert any("file d'écriture pleine" in r.getMessage() for r in caplog.records)
    assert top._FILE.qsize() == 1


def test_un_echec_d_ecriture_ne_leve_pas(monkeypatch, caplog):
    from oto_mcp.db import limite_refus_jour as db_refus

    def boom(_lignes):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(db_refus, "ajouter", boom)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_refus_top"):
        top._ecrire([{"cle": "k"}])
    assert any("en échec" in r.getMessage() for r in caplog.records)


def test_l_ecriture_trie_par_cle_primaire_et_borne_sa_duree(monkeypatch):
    """Deux processus qui écrivent la même minute verrouillent dans le même ordre."""
    from contextlib import contextmanager
    from oto_mcp.db import limite_refus_jour as db_refus
    vu = {"executes": [], "many": None}

    class Curseur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def executemany(self, sql, lignes):
            vu["many"] = (sql, list(lignes))

    class Conn:
        def execute(self, sql, *a):
            vu["executes"].append(sql)

        @contextmanager
        def transaction(self):
            yield

        def cursor(self):
            return Curseur()

    @contextmanager
    def connect():
        yield Conn()

    monkeypatch.setattr(db_refus, "_connect", connect)
    base = {"jour": date(2025, 10, 9), "env": "e", "limiteur": "debit",
            "classe": "anonyme"}
    db_refus.ajouter([{**base, "cle": "b"}, {**base, "cle": "a"}])
    assert [l["cle"] for l in vu["many"][1]] == ["a", "b"]
    assert "ON CONFLICT (jour, env, limiteur, classe, cle)" in vu["many"][0]
    assert any("statement_timeout" in s for s in vu["executes"])
