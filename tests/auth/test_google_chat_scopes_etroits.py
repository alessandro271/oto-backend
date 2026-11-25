"""Google Chat aux scopes étroits (23/11/2026), et le refresh des lignes d'avant.

Chat demandait `chat.spaces.readonly` + `chat.messages`. Le client n'appelle que
`spaces.list`, `spaces.findDirectMessage`, `spaces.messages.list` et
`spaces.messages.create` : aucune modification, suppression, réaction ni gestion des
membres. Le jeu devient `chat.spaces.readonly` + `chat.messages.readonly` +
`chat.messages.create`, et `chat.messages` quitte la déclaration du projet OAuth.

Ce que ce banc tient :
1. le jeu de Chat, exact, et que `chat.messages` n'est plus connu du coffre ;
2. une ligne d'avant (qui enregistre `chat.messages`) ne le RENVOIE jamais au refresh :
   la borne envoyée est l'intersection avec ce que le code déclare ; Gmail et les autres
   services de la ligne continuent de servir ;
3. cette ligne ne couvre plus Chat : refus nommé, sans appel réseau ;
4. un bornage refusé par Google (`invalid_scope`, p. ex. `tasks` retiré de la console
   alors que le code le connaît) réessaie UNE fois, borné au service appelé — jamais
   sans borne —, et le jeton ainsi tiré n'est jamais servi à un autre service.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import google as G

BASE = "https://www.googleapis.com/auth/"
IDENTITE = " ".join(G.IDENTITY_SCOPES)
GMAIL = BASE + "gmail.modify"
DRIVE = BASE + "drive"
TASKS = BASE + "tasks"
ANCIEN_CHAT = BASE + "chat.messages"
# Une ligne consentie avant le 23/11/2026 par la carte du compte : six services,
# dont l'ancienne paire de Chat.
LIGNE_ANCIENNE = " ".join([*G.IDENTITY_SCOPES, BASE + "spreadsheets", DRIVE, GMAIL, TASKS,
                           BASE + "calendar", BASE + "chat.spaces.readonly", ANCIEN_CHAT])


class _Rep:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture()
def env(monkeypatch):
    from _coffre_google import installer

    e = installer(monkeypatch, org=7, sub="sub")
    monkeypatch.setattr(G.connector_health, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(G.connector_health, "mark_rejected", lambda *a, **k: None)
    return e


@pytest.fixture()
def google(monkeypatch):
    """Le point de jeton de Google : note chaque `scope` envoyé ; refuse (`invalid_scope`)
    toute borne qui contient un scope de `refuses`."""
    import requests
    vu = {"scopes": [], "refuses": set()}

    def _post(url, data=None, timeout=None):
        envoye = (data or {}).get("scope")
        vu["scopes"].append(envoye)
        if envoye and vu["refuses"] & set(envoye.split()):
            return _Rep(400, '{"error":"invalid_scope"}')
        return _Rep(200, payload={"access_token": f"at-{len(vu['scopes'])}",
                                  "expires_in": 3600})

    monkeypatch.setattr(requests, "post", _post)
    return vu


# ─── 1. le jeu de Chat ────────────────────────────────────────────────────────

def test_chat_demande_trois_scopes_etroits_et_plus_chat_messages():
    assert G.SERVICE_SCOPES["chat"] == (
        BASE + "chat.spaces.readonly",
        BASE + "chat.messages.readonly",
        BASE + "chat.messages.create",
    )
    assert ANCIEN_CHAT not in G.KNOWN_SCOPES
    # La carte de Chat ne demande que ça (et l'identité).
    app = G.OAuthApp(client_id="cid", client_secret="s", redirect_uri="https://x/cb")
    assert G.scopes_for("chat", app) == [*G.IDENTITY_SCOPES, *G.SERVICE_SCOPES["chat"]]


def test_un_nouveau_consentement_n_enregistre_pas_l_ancien_scope(monkeypatch):
    """`include_granted_scopes` : Google rend l'union, `chat.messages` compris. Le coffre
    n'en garde que ce que le code déclare."""
    ecrit = {}
    monkeypatch.setattr(G.db, "set_google_oauth", lambda *a, **k: ecrit.update(k))
    monkeypatch.setattr(G, "_fetch_email", lambda at, scopes=(): "a@b.com")
    union = " ".join([LIGNE_ANCIENNE, *G.SERVICE_SCOPES["chat"][1:]])
    G.persist_token("sub", 7, {"refresh_token": "RT", "access_token": "at",
                               "scope": union}, client_id="cid")
    assert ANCIEN_CHAT not in ecrit["scopes"].split()
    assert "chat" in G.services_granted(ecrit["scopes"])


# ─── 2. une ligne d'avant ne renvoie jamais l'ancien scope ─────────────────────

def test_la_borne_est_l_intersection_avec_ce_que_le_code_declare():
    borne = G._declared_scopes(LIGNE_ANCIENNE)
    assert ANCIEN_CHAT not in borne.split()
    assert borne.split() == [sc for sc in LIGNE_ANCIENNE.split() if sc != ANCIEN_CHAT]
    assert G._declared_scopes(None) is None and G._declared_scopes("") == ""
    # Rien de déclaré : la ligne telle quelle, jamais une borne vide (= sans borne).
    assert G._declared_scopes(ANCIEN_CHAT) == ANCIEN_CHAT


def test_une_ligne_d_avant_rafraichit_gmail_sans_l_ancien_scope(env, google):
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=LIGNE_ANCIENNE,
                     access_token=None, client_id="cid-env")
    creds = G.credentials_for("sub", account="a@b.com", service="gmail")
    assert len(google["scopes"]) == 1
    envoye = google["scopes"][0].split()
    assert ANCIEN_CHAT not in envoye and GMAIL in envoye and DRIVE in envoye
    # google-auth renverrait ces scopes à SON refresh : la même borne.
    assert set(creds.scopes) == set(envoye)
    # Le jeton est gardé sous cette borne : l'appel suivant ne rafraîchit pas.
    G.credentials_for("sub", account="a@b.com", service="drive")
    assert len(google["scopes"]) == 1


# ─── 3. elle ne couvre plus Chat ──────────────────────────────────────────────

def test_une_ligne_d_avant_ne_couvre_plus_chat_sans_appel_reseau(env, google):
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=LIGNE_ANCIENNE,
                     access_token=None, client_id="cid-env")
    with pytest.raises(RuntimeError) as e:
        G.credentials_for("sub", account="a@b.com", service="chat")
    assert "Google Chat" in str(e.value) and "Nothing was done" in str(e.value)
    assert google["scopes"] == []


# ─── 4. bornage refusé : un seul réessai, borné au service appelé ─────────────

def test_un_bornage_refuse_reessaie_borne_au_service_appele(env, google):
    google["refuses"] = {TASKS}
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=LIGNE_ANCIENNE,
                     access_token=None, client_id="cid-env")
    creds = G.credentials_for("sub", account="a@b.com", service="gmail")
    assert len(google["scopes"]) == 2
    assert TASKS in google["scopes"][0].split()
    assert sorted(google["scopes"][1].split()) == sorted([*G.IDENTITY_SCOPES, GMAIL])
    assert sorted(creds.scopes) == sorted([*G.IDENTITY_SCOPES, GMAIL])
    # Gmail ressert le jeton borné à Gmail, sans réseau…
    G.credentials_for("sub", account="a@b.com", service="gmail")
    assert len(google["scopes"]) == 2
    # … mais Drive ne le reçoit jamais : il tire le sien, borné à Drive.
    creds_drive = G.credentials_for("sub", account="a@b.com", service="drive")
    assert creds_drive.token != creds.token
    assert sorted(google["scopes"][-1].split()) == sorted([*G.IDENTITY_SCOPES, DRIVE])


def test_un_bornage_refuse_deux_fois_est_un_refus_nomme_jamais_sans_borne(env, google):
    google["refuses"] = {GMAIL}
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=LIGNE_ANCIENNE,
                     access_token=None, client_id="cid-env")
    with pytest.raises(G.GoogleScopeRejected) as e:
        G.credentials_for("sub", account="a@b.com", service="gmail")
    assert "reconnect this account" in str(e.value)
    assert len(google["scopes"]) == 2 and all(google["scopes"])


def test_rien_de_plus_etroit_a_essayer_pas_de_reessai(env, google):
    """Une ligne qui ne porte que Gmail : la borne du service EST la borne de la ligne."""
    google["refuses"] = {GMAIL}
    env.coffre.poser("a@b.com", "rt", defaut=True, scopes=f"{IDENTITE} {GMAIL}",
                     access_token=None, client_id="cid-env")
    with pytest.raises(G.GoogleScopeRejected):
        G.credentials_for("sub", account="a@b.com", service="gmail")
    assert len(google["scopes"]) == 1
