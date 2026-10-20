"""Le préréglage SLACK d'un webhook (Events API) et le FILTRE de livraison.

Une app Slack abonnée à l'Events API POSTe chaque événement à la « Request URL »
de son écran *Event Subscriptions* : un agent `hook_auth=slack` EST cette URL.
Ce fichier tient les propriétés qui comptent :

1. **La signature `v0`** se vérifie contre le vecteur PUBLIÉ par Slack, sur les
   octets reçus, à temps constant, dans une fenêtre de ±5 min.
2. **Chacun sa porte** : ni le porteur ni une signature Standard Webhooks
   n'ouvrent un agent `slack`, et une signature Slack n'ouvre rien d'autre ; tout
   échec d'identité rend le 404 commun, non journalisé.
3. **La poignée de main** (`url_verification`) n'est rendue qu'après une signature
   valide — même sur un agent en pause, sinon on ne pourrait jamais le brancher.
4. **Le bruit ne lance jamais d'agent** — un bot (l'agent lui-même, qui poste dans
   un canal où son app écoute) se redéclencherait en boucle ; et ce qui est écarté
   rend un SUCCÈS, jamais un refus que Slack retenterait.
5. **`event_id` déduplique** les retentatives de Slack.
6. **Le filtre** écarte à la porte, sans run, ce qu'une source envoie en trop ; il
   distingue `true` de `1`, et se refuse mal formé à la pose.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest

from oto_mcp import hook_filter, hook_slack, runner_hook
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77
#: Un Signing Secret de test, de la forme de ceux que Slack affiche (hexadécimal).
SECRET = "0123456789abcdef0123456789abcdef"


def _signer(secret: str, ts: str, corps: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + corps,
                            hashlib.sha256).hexdigest()


def _evt(event=None, **enveloppe) -> dict:
    corps = {"type": "event_callback", "event_id": "Ev001", "team_id": "T0001",
             "is_ext_shared_channel": True,
             "event": event if event is not None else
             {"type": "message", "channel": "C0001", "user": "U0001",
              "text": "bonjour", "ts": "1700000000.000100"}}
    corps.update(enveloppe)
    return corps


def _recue(corps: dict, ts=None, secret=SECRET):
    brut = json.dumps(corps).encode()
    ts = ts or str(int(time.time()))
    return corps, hook_slack.SignatureSlack(ts, _signer(secret, ts, brut), brut)


# ── 1. la SIGNATURE ───────────────────────────────────────────────────────────

def test_le_vecteur_PUBLIE_par_Slack_passe():
    """https://api.slack.com/authentication/verifying-requests-from-slack —
    l'exemple de la documentation, octet pour octet."""
    corps = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain="
             b"testteamnow&channel_id=G8PSS9T3V&channel_name=foobar&user_id="
             b"U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&"
             b"response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J"
             b"%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&trigger_id=398738663015."
             b"47445629121.803a0bc887a14d10d2c447fce8b6703c")
    r = hook_slack.SignatureSlack(
        "1531420618",
        "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503",
        corps)
    assert hook_slack.verifier("8f742231b10e8888abcd99yyyzzz85a5", r,
                               maintenant=1531420618) == "ok"


def test_un_corps_RE_SERIALISE_ne_passe_pas():
    corps, r = _recue(_evt())
    autre = hook_slack.SignatureSlack(r.horodatage, r.signature,
                                      json.dumps(corps, indent=1).encode())
    assert hook_slack.verifier(SECRET, autre) == "invalid"


def test_un_AUTRE_secret_rend_invalid():
    _, r = _recue(_evt(), secret="f" * 32)
    assert hook_slack.verifier(SECRET, r) == "invalid"


def test_hors_fenetre_rend_STALE_seulement_pour_une_signature_VALIDE():
    vieux = str(int(time.time()) - 3600)
    _, r = _recue(_evt(), ts=vieux)
    assert hook_slack.verifier(SECRET, r) == "stale"
    _, faux = _recue(_evt(), ts=vieux, secret="f" * 32)
    assert hook_slack.verifier(SECRET, faux) == "invalid"


def test_les_entetes_vont_par_DEUX_ou_pas_du_tout():
    assert hook_slack.signature_des_entetes({"x-slack-signature": "v0=a"}, b"") is None
    assert hook_slack.signature_des_entetes(
        {"x-slack-request-timestamp": "1"}, b"") is None
    lue = runner_hook.signature_des_entetes(
        {"x-slack-signature": "v0=a", "x-slack-request-timestamp": "1"}, b"{}")
    assert isinstance(lue, hook_slack.SignatureSlack)


@pytest.mark.parametrize("secret,ok", [
    (SECRET, True), ("A" * 32, False), ("xoxb-123-456", False),
    ("whsec_" + "a" * 32, False), ("otoh_" + "a" * 32, False), ("abc", False),
    (None, False)])
def test_la_forme_du_Signing_Secret(secret, ok):
    assert hook_slack.secret_valide(secret) is ok


# ── 2. la ROUTE logique : porte, poignée de main, bruit, doublon ───────────────

class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def slack(monkeypatch):
    """Un agent `slack` ; `trigger_signe` rejoue la garde SQL du mode."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    vu = {"livraisons": [], "deja": None, "signe": []}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "support",
         "tools": ["a"], "input": "réponds", "enabled": True, "kind": "webhook",
         "payload_mode": "inline", "payload_fields": None, "max_per_hour": None,
         "fraicheur_s": None, "model": None, "project_id": None, "max_steps": None,
         "label": None, "hook_auth": "slack", "max_per_day": None,
         "hook_slug": "h_privee", "hook_filter": None,
         "hook_signing_secret_enc": runner_hook.chiffrer_secret_de_signature(5, SECRET)}
    vu["trigger"] = t

    def _signe(i, hook_auth="standard_webhooks"):
        vu["signe"].append(hook_auth)
        return dict(t) if i == 5 and t["hook_auth"] == hook_auth else None

    monkeypatch.setattr(runner_hook.db, "trigger_signe", _signe)
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret",
                        lambda i, h, hook_auth="bearer": None)
    monkeypatch.setattr(runner_hook.db, "trigger_sans_preuve", lambda i: None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn())
    monkeypatch.setattr(runner_hook.db, "verrouiller_le_declencheur", lambda c, i: None)
    monkeypatch.setattr(runner_hook.db, "retard_de_lissage", lambda c, t_, d, f: 0)
    monkeypatch.setattr(runner_hook.db, "acceptees_sur_24h", lambda c, i: (0, 3600))
    monkeypatch.setattr(runner_hook.db, "livraison_acceptee",
                        lambda c, t_, e: vu["deja"] if vu["deja"] and
                        vu["deja"]["external_id"] == e else None)
    monkeypatch.setattr(
        runner_hook.db, "enregistrer",
        lambda c, t_, o, outcome, job_id=None, source=None, due_at=None,
        external_id=None: vu["livraisons"].append((outcome, external_id)) or 1)
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda org, kind, **kw: vu.update(enfile=kw) or {"id": 900})
    monkeypatch.setattr(runner_hook.org_suspension, "etat", lambda o: None)
    monkeypatch.setattr(runner_hook.procedure_retiree, "archivee", lambda *a: None)
    return vu


def _declencher(corps, sig, tid=5, secret=None, adresse=True):
    return runner_hook.declencher(tid, secret, corps, "Slackbot 1.0",
                                  signature=sig, par_adresse_privee=adresse)


def test_un_message_signe_ENFILE_et_garde_l_event_id(slack):
    out = _declencher(*_recue(_evt()))
    assert out["job_id"] == 900 and not out.get("ignored")
    assert slack["livraisons"] == [(runner_hook.db.QUEUED, "Ev001")]
    assert "bonjour" in slack["enfile"]["payload"]["input"]
    assert slack["signe"] == ["slack"], "la signature Slack ne cherche QUE son mode"


def test_la_poignee_de_main_est_rendue_apres_la_signature_SANS_travail(slack):
    out = _declencher(*_recue({"type": "url_verification", "challenge": "abc123",
                               "token": "x"}))
    assert out["challenge"] == "abc123"
    assert "enfile" not in slack and slack["livraisons"] == []


def test_la_poignee_de_main_passe_meme_sur_un_agent_en_PAUSE(slack):
    """Sinon un agent posé éteint ne pourrait jamais être branché."""
    slack["trigger"]["enabled"] = False
    out = _declencher(*_recue({"type": "url_verification", "challenge": "abc"}))
    assert out["challenge"] == "abc"


def test_une_poignee_de_main_MAL_signee_n_est_pas_un_echo(slack):
    corps, r = _recue({"type": "url_verification", "challenge": "abc"},
                      secret="f" * 32)
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(corps, r)
    assert (e.value.statut, e.value.message) == (404, runner_hook.HOOK_INCONNU)


def test_signature_fausse_et_id_inconnu_rendent_LE_MEME_404_sans_journal(slack):
    refus = []
    for tid, (corps, sig) in ((5, _recue(_evt(), secret="f" * 32)), (6, _recue(_evt()))):
        with pytest.raises(runner_hook.HookRefus) as e:
            _declencher(corps, sig, tid=tid)
        refus.append((e.value.statut, e.value.code, e.value.message))
    assert refus[0] == refus[1] == (404, "hook_not_found", runner_hook.HOOK_INCONNU)
    assert slack["livraisons"] == []


def test_un_REJEU_hors_fenetre_est_nomme_et_journalise(slack):
    corps, r = _recue(_evt(), ts=str(int(time.time()) - 3600))
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(corps, r)
    assert (e.value.statut, e.value.code) == (400, "hook_stale_timestamp")
    assert slack["livraisons"] == [(runner_hook.db.REFUSE_STALE, "Ev001")]


def test_le_PORTEUR_n_ouvre_pas_un_agent_slack(slack):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_un_porteur", _evt(), "curl",
                               par_adresse_privee=True)
    assert e.value.statut == 404


def test_une_signature_STANDARD_WEBHOOKS_n_ouvre_pas_un_agent_slack(slack):
    sw = runner_hook.SignatureRecue("evt_1", str(int(time.time())), "v1,xx", b"{}")
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher({}, sw)
    assert e.value.statut == 404
    assert slack["signe"] == ["standard_webhooks"]


def test_une_signature_SLACK_n_ouvre_pas_un_agent_standard_webhooks(slack):
    slack["trigger"]["hook_auth"] = "standard_webhooks"
    with pytest.raises(runner_hook.HookRefus):
        _declencher(*_recue(_evt()))


def test_par_l_id_NUMERIQUE_d_un_agent_a_adresse_privee_est_refuse(slack):
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(*_recue(_evt()), adresse=False)
    assert e.value.statut == 404


@pytest.mark.parametrize("evenement,motif", [
    ({"type": "message", "bot_id": "B0001", "text": "mon propre post"}, "bot_message"),
    ({"type": "message", "subtype": "bot_message", "text": "x"}, "bot_message"),
    ({"type": "message", "subtype": "message_changed"}, "not_a_new_message"),
    ({"type": "message", "subtype": "message_deleted"}, "not_a_new_message"),
    ({"type": "message", "subtype": "channel_join", "user": "U1"}, "not_a_new_message"),
    ({"type": "message", "hidden": True}, "hidden"),
])
def test_le_BRUIT_ne_lance_jamais_d_agent_et_rend_un_succes(slack, evenement, motif):
    out = _declencher(*_recue(_evt(evenement)))
    assert out["ignored"] == motif and out["job_id"] is None
    assert "enfile" not in slack
    assert slack["livraisons"] == [], "un canal actif ne remplit pas le journal"


def test_une_enveloppe_qui_n_est_pas_un_evenement_est_ecartee(slack):
    out = _declencher(*_recue({"type": "app_rate_limited", "minute_rate_limited": 1}))
    assert out["ignored"] == "not_an_event" and "enfile" not in slack


@pytest.mark.parametrize("sous_type", ["file_share", "thread_broadcast"])
def test_un_humain_qui_joint_un_fichier_ou_diffuse_un_fil_PASSE(slack, sous_type):
    out = _declencher(*_recue(_evt({"type": "message", "subtype": sous_type,
                                    "user": "U1", "text": "voir capture"})))
    assert out["job_id"] == 900


def test_une_RETENTATIVE_du_meme_event_id_ne_refait_pas_de_deroule(slack):
    slack["deja"] = {"id": 1, "job_id": 900, "external_id": "Ev001"}
    out = _declencher(*_recue(_evt()))
    assert out["duplicate"] is True and "enfile" not in slack


def test_un_agent_slack_en_PAUSE_journalise_et_ACQUITTE_en_200(slack):
    """Slack lit tout non-2xx comme un échec : retenté trois fois, puis
    l'abonnement coupé. Mettre l'agent en pause ne doit pas casser son
    branchement — le refus est journalisé UNE fois et rendu en 200."""
    slack["trigger"]["enabled"] = False
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(*_recue(_evt()))
    assert (e.value.statut, e.value.code) == (200, "trigger_paused")
    assert slack["livraisons"] == [(runner_hook.db.REFUSE_PAUSED, "Ev001")]


def test_le_Verification_Token_et_les_installations_n_atteignent_PAS_l_agent(slack):
    corps = _evt(token="legacy-verification-token-xyz",
                 authorizations=[{"team_id": "T0001", "user_id": "U0BOT", "is_bot": True}])
    out = _declencher(*_recue(corps))
    assert out["job_id"] == 900
    entree = slack["enfile"]["payload"]["input"]
    assert "legacy-verification-token-xyz" not in entree
    assert "authorizations" not in entree and "bonjour" in entree


def test_le_filtre_lit_l_enveloppe_ENTIERE_avant_le_retrait(slack):
    slack["trigger"]["hook_filter"] = {"authorizations.0.is_bot": [True]}
    corps = _evt(authorizations=[{"user_id": "U0BOT", "is_bot": True}])
    assert _declencher(*_recue(corps))["job_id"] == 900


@pytest.mark.parametrize("evenement,enveloppe", [
    # posté par NOTRE app avec un jeton utilisateur : pas de `bot_id`
    ({"type": "message", "user": "U0HUMAIN", "app_id": "A0APP", "text": "réponse"},
     {"api_app_id": "A0APP"}),
    # une réaction posée par l'utilisateur-bot de l'app
    ({"type": "reaction_added", "user": "U0BOT", "reaction": "eyes"},
     {"authorizations": [{"user_id": "U0BOT", "is_bot": True}]}),
    # un message de bot qui ne porte que `bot_profile`
    ({"type": "message", "user": "U0X", "bot_profile": {"id": "B1"}, "text": "x"}, {}),
])
def test_ce_que_NOTRE_app_fait_ne_relance_jamais_l_agent(slack, evenement, enveloppe):
    out = _declencher(*_recue(_evt(evenement, **enveloppe)))
    assert out.get("ignored") in ("own_app", "bot_message") and "enfile" not in slack


def test_le_message_d_une_AUTRE_app_par_un_humain_passe(slack):
    out = _declencher(*_recue(_evt({"type": "message", "user": "U0H", "text": "x",
                                    "app_id": "A0AUTRE"}, api_app_id="A0APP")))
    assert out["job_id"] == 900


# ── 3. le FILTRE ──────────────────────────────────────────────────────────────

def test_le_filtre_ECARTE_sans_travail_et_laisse_passer_le_reste(slack):
    slack["trigger"]["hook_filter"] = {"is_ext_shared_channel": [True]}
    out = _declencher(*_recue(_evt(is_ext_shared_channel=False)))
    assert out["ignored"] == "filtered" and "enfile" not in slack
    assert slack["livraisons"] == []
    out = _declencher(*_recue(_evt(event_id="Ev002")))
    assert out["job_id"] == 900


def test_le_filtre_s_applique_a_TOUT_prereglage(slack, monkeypatch):
    """Un agent au PORTEUR : le filtre écarte ce qui ne correspond pas, et laisse
    partir ce qui correspond (les deux sens, sinon le test ne prouve rien)."""
    t = dict(slack["trigger"], hook_auth="bearer",
             hook_filter={"data.kind": ["lead"]})
    monkeypatch.setattr(runner_hook, "_authentifier",
                        lambda tid, s, sig, src, adr=False, corps=None: (t, None, corps))
    out = runner_hook.declencher(5, "otoh_x", {"data": {"kind": "invoice"}}, "x")
    assert out["ignored"] == "filtered" and "enfile" not in slack
    out = runner_hook.declencher(5, "otoh_x", {"data": {"kind": "lead"}}, "x")
    assert out["job_id"] == 900 and not out.get("ignored")


@pytest.mark.parametrize("filtre,corps,passe", [
    ({"a": [True]}, {"a": True}, True),
    ({"a": [True]}, {"a": 1}, False),
    ({"a": [1]}, {"a": True}, False),
    ({"a": ["true"]}, {"a": True}, False),
    ({"a.b": ["x", "y"]}, {"a": {"b": "y"}}, True),
    ({"a.b": ["x"]}, {"a": {}}, False),
    ({"a.0": ["x"]}, {"a": ["x"]}, True),
    ({"$.a": [None]}, {"a": None}, True),
    ({"a": ["x"], "b": ["y"]}, {"a": "x", "b": "z"}, False),
    (None, {"rien": 1}, True),
])
def test_la_correspondance_du_filtre(filtre, corps, passe):
    assert hook_filter.correspond(filtre, corps) is passe


@pytest.mark.parametrize("filtre", [
    [], {"a": []}, {"a": "x"}, {"": ["x"]}, {"a": [{"b": 1}]},
    {f"k{i}": ["x"] for i in range(11)}, {"a": ["x" * 201]},
    {"a": [float("nan")]}, {"a": [float("inf")]},
])
def test_un_filtre_MAL_FORME_est_refuse(filtre):
    assert hook_filter.probleme(filtre)


# ── 4. la POSE : mode, secret, filtre ─────────────────────────────────────────

def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "support", "tools": ["a"], "enabled": True,
             "payload_mode": "inline", "payload_fields": None,
             "model": "claude-sonnet-5", "hook_auth": "bearer",
             "signing_secret_set": False, "hook_filter": None}
    etat = {"ligne": ligne, "hash": "h0", "enc": None}

    def _poser_auth(tid, org, mode, secret_enc=None, effacer_le_secret=False):
        ligne["hook_auth"] = mode
        if effacer_le_secret:
            etat["enc"] = None
        elif secret_enc is not None:
            etat["enc"] = secret_enc
        ligne["signing_secret_set"] = etat["enc"] is not None
        return True

    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda t, o, champs, hors_abonnement_d_autrui=None:
                        etat.update(ecrit=champs) or ligne.update(champs) or dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", _poser_auth)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: etat.update(hash=h) or True)
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    monkeypatch.setattr(RT.db, "file_du_declencheur",
                        lambda t, o: {"pending": 0, "held": 0})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})
    return etat


def _auth(**kw):
    return asyncio.run(RT._hook_auth(_ctx(), RT.HookAuthInput(trigger_id=5, **kw)))


def test_passer_en_slack_pose_le_secret_CHIFFRE_et_EFFACE_le_porteur(base):
    out = _auth(hook_auth="slack", signing_secret=SECRET)
    assert out["trigger"]["hook_auth"] == "slack"
    assert out["trigger"]["signing_secret_set"] is True
    assert SECRET not in repr(out)
    assert base["enc"] and SECRET not in base["enc"]
    assert base["hash"] is None, "un otoh_ fuité ne doit pas se réveiller plus tard"
    assert runner_hook._dechiffrer_secret_de_signature(5, base["enc"]) == SECRET


def test_slack_SANS_secret_est_refuse(base):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="slack")
    assert e.value.code == "missing_signing_secret"


@pytest.mark.parametrize("faux", ["xoxb-1-2-abc", "whsec_" + "a" * 32, "otoh_abc"])
def test_slack_refuse_ce_qui_n_est_pas_un_Signing_Secret(base, faux):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="slack", signing_secret=faux)
    assert e.value.code == "invalid_signing_secret" and base["enc"] is None


def test_un_Signing_Secret_Slack_n_est_pas_un_whsec(base):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    assert e.value.code == "invalid_signing_secret"


def test_passer_de_standard_webhooks_a_slack_EXIGE_le_secret_du_nouveau_mode(base):
    """Un `whsec_` gardé ne vérifierait aucune signature Slack : l'agent aurait
    l'air branché en refusant tout."""
    base["ligne"].update(hook_auth="standard_webhooks", signing_secret_set=True)
    base["enc"] = "chiffre_d_un_whsec"
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="slack")
    assert e.value.code == "missing_signing_secret"


def test_rester_en_slack_garde_le_secret_deja_pose(base):
    base["ligne"].update(hook_auth="slack", signing_secret_set=True)
    out = _auth(hook_auth="slack")
    assert out["trigger"]["hook_auth"] == "slack"


def test_le_filtre_se_POSE_et_se_RETIRE_par_update(base, monkeypatch):
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": None, "families": ["anthropic"]})
    f = {"is_ext_shared_channel": [True]}
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=5,
                                                      hook_filter=f)))
    assert base["ecrit"]["hook_filter"] == f
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=5,
                                                      hook_filter={})))
    assert base["ecrit"]["hook_filter"] is None, "`{}` RETIRE le filtre : NULL"


def test_un_filtre_mal_forme_est_refuse_a_la_POSE(base):
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(
            op="update", trigger_id=5, hook_filter={"a": []})))
    assert e.value.code == "invalid_hook_filter"


def test_le_filtre_sur_un_agent_PROGRAMME_est_refuse(base):
    base["ligne"]["kind"] = "schedule"
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(
            op="update", trigger_id=5, hook_filter={"a": ["x"]})))
    assert e.value.code == "not_a_webhook"


def test_trigger_signe_ne_cherche_QUE_les_modes_signes(monkeypatch):
    from oto_mcp.db import runner_triggers as dbt

    def _jamais():
        raise AssertionError("aucune requête pour un mode qui ne signe pas")

    monkeypatch.setattr(dbt, "_connect", _jamais)
    assert dbt.trigger_signe(5, "bearer") is None
    assert dbt.trigger_signe(5, "none") is None


# ── 5. la ROUTE HTTP ──────────────────────────────────────────────────────────

def _app(monkeypatch, rendu):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import hooks

    vu = {}

    def _declencher(tid, secret, corps, source, *, signature=None,
                    par_adresse_privee=False):
        vu.update(sig=signature, corps=corps)
        return rendu

    monkeypatch.setattr(hooks.runner_hook, "declencher", _declencher)
    monkeypatch.setattr(hooks.runner_hook, "resoudre_adresse", lambda s: (5, True))
    return TestClient(Starlette(routes=hooks.make_routes(lambda r: None))), vu


def test_la_route_rend_le_CHALLENGE_en_200_et_verifie_les_OCTETS(monkeypatch):
    client, vu = _app(monkeypatch, {"ok": True, "trigger_id": 5, "challenge": "c-42"})
    corps = b'{"type": "url_verification",  "challenge":"c-42"}'
    ts = str(int(time.time()))
    r = client.post("/api/hooks/h_x", content=corps, headers={
        "x-slack-request-timestamp": ts,
        "x-slack-signature": _signer(SECRET, ts, corps),
        "content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {"challenge": "c-42"}
    assert isinstance(vu["sig"], hook_slack.SignatureSlack) and vu["sig"].brut == corps
    assert hook_slack.verifier(SECRET, vu["sig"]) == "ok"


def test_la_route_rend_un_SUCCES_200_pour_ce_qui_est_ecarte(monkeypatch):
    client, _ = _app(monkeypatch, {"ok": True, "trigger_id": 5, "job_id": None,
                                   "ignored": "bot_message"})
    r = client.post("/api/hooks/h_x", json={"type": "event_callback"})
    assert r.status_code == 200 and r.json()["ignored"] == "bot_message"
