"""Ce que `whatsapp_chat` (et `telegram_chat`, `instagram_chat`) sert : la page Unipile,
remise en forme pour qu'un agent la lise telle quelle (signaux WhatsApp du 25/09 au
05/10/2026 : 1129, 1175, 1176, 1177, 1178, 1214, 1215, 1354, 1357, 1358, 1359).

Les identifiants des bancs sont des formes, jamais de vrais numéros ni de vrais comptes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import unipile_messagerie as M

GROUPE = "120000000000000001@g.us"          # forme d'un JID de groupe (18 chiffres)
LID = "111111111111111@lid"                 # forme d'un identifiant de confidentialité
TEL = "33600000000@s.whatsapp.net"          # forme d'un vrai numéro


def _page(items, **env):
    """La page telle qu'oto-core la rend : `data` ET `items`, `next_cursor` ET `cursor`."""
    return {"object": "ChatList", "data": items, "items": items, **env}


# --- une seule clé de liste (1177, 1214, 1357) --------------------------------

def test_la_liste_n_est_servie_qu_une_fois():
    out = M.page_de_fils(_page([{"id": "c1"}]), limit=20)
    assert "data" not in out and out["items"] == [{"id": "c1"}]


def test_un_curseur_unipile_est_rendu_sous_une_seule_cle():
    out = M.page_de_fils(_page([{"id": "c1"}], next_cursor="K", cursor="K"), limit=1)
    assert out["cursor"] == "K" and "next_cursor" not in out
    assert "truncated" not in out


def test_les_messages_aussi_n_ont_qu_une_cle():
    out = M.page_de_messages(_page([{"id": "m1", "text": "salut"}]))
    assert "data" not in out and out["items"] == [{"id": "m1", "text": "salut"}]


# --- la borne et l'absence de curseur (1214, 1215) ----------------------------

def test_une_page_pleine_sans_curseur_dit_qu_elle_n_est_pas_tout():
    out = M.page_de_fils(_page([{"id": f"c{i}"} for i in range(3)]), limit=3)
    assert out["truncated"] is True
    assert "NOT reachable" in out["truncated_note"]
    assert "cursor" not in out


def test_une_page_incomplete_ne_dit_rien():
    out = M.page_de_fils(_page([{"id": "c1"}]), limit=20)
    assert "truncated" not in out


@pytest.mark.parametrize("limit", [0, 101, 500])
def test_une_page_que_unipile_refuserait_est_refusee_avant(limit):
    assert "1 to 100" in M.borne_de_liste(limit)


@pytest.mark.parametrize("limit", [None, 1, 100])
def test_la_borne_elle_meme_passe(limit):
    assert M.borne_de_liste(limit) is None


# --- un fil listé deux fois (1178) --------------------------------------------

def test_un_fil_liste_deux_fois_n_est_garde_qu_une_fois():
    items = [{"id": "c1", "timestamp": "2026-09-25"}, {"id": "c2"},
             {"id": "c1", "timestamp": "2026-08-28"}]
    out = M.page_de_fils(_page(items), limit=20)
    assert [i["id"] for i in out["items"]] == ["c1", "c2"]
    assert out["items"][0]["timestamp"] == "2026-09-25", "le premier vu, le plus récent"
    assert out["duplicates_removed"] == 1


# --- des identifiants habillés en numéros (1176) ------------------------------

def test_le_jid_d_un_groupe_n_est_pas_un_numero():
    msg = {"id": "m1", "is_event": True,
           "sender": {"id": GROUPE, "display_name": "+12 0 00 00 00 000000001"}}
    out = M.page_de_messages(_page([msg]))
    assert out["items"][0]["sender"]["display_name"] is None
    assert out["items"][0]["sender"]["id"] == GROUPE, "l'identifiant reste"
    assert out["false_numbers_removed"] == 1


def test_un_identifiant_de_confidentialite_n_est_pas_un_numero():
    fil = {"id": "c1", "last_message": {"sender_id": LID,
                                        "sender_display_name": "+11 1 11 11 11 111111"}}
    out = M.page_de_fils(_page([fil]), limit=20)
    assert out["items"][0]["last_message"]["sender_display_name"] is None
    assert "never phone numbers" in out["false_numbers_note"]


def test_un_vrai_numero_n_est_jamais_touche():
    fil = {"id": "c1", "name": "+33 6 00 00 00 00",
           "attendee": {"id": TEL, "display_name": "+33 6 00 00 00 00"}}
    out = M.page_de_fils(_page([fil]), limit=20)
    assert out["items"][0]["name"] == "+33 6 00 00 00 00"
    assert out["items"][0]["attendee"]["display_name"] == "+33 6 00 00 00 00"
    assert "false_numbers_removed" not in out


def test_un_nom_qui_n_est_pas_un_numero_n_est_jamais_touche():
    msg = {"id": "m1", "sender": {"id": LID, "display_name": "Camille"}, "text": "x"}
    out = M.page_de_messages(_page([msg]))
    assert out["items"][0]["sender"]["display_name"] == "Camille"


# --- un message sans contenu transmis (1175, 1215) et les pièces jointes (1129) ---

def test_un_message_sans_texte_ni_piece_jointe_est_dit_illisible():
    msgs = [{"id": "m1", "text": "", "attachments": []},
            {"id": "m2", "text": "bonjour", "attachments": []},
            {"id": "m3", "is_event": True}]
    out = M.page_de_messages(_page(msgs))
    assert out["items"][0]["content_unavailable"] is True
    assert "content_unavailable" not in out["items"][1]
    assert "content_unavailable" not in out["items"][2], "un événement n'a pas de texte"
    assert out["content_unavailable"]["count"] == 1
    assert "unknown, not empty" in out["content_unavailable"]["reason"]


def test_une_piece_jointe_dit_que_son_url_exige_la_cle():
    msgs = [{"id": "m1", "text": "", "attachments": [{"url": "https://unipile.example/x"}]}]
    out = M.references_des_pieces(M.page_de_messages(_page(msgs)), "whatsapp")
    assert "content_unavailable" not in out["items"][0]
    assert "requires its key" in out["attachments_note"]


# --- récupérer une pièce jointe : une RÉFÉRENCE, jamais les octets (#1149) ---------

def test_chaque_piece_porte_la_reference_que_file_source_resout():
    url = "https://unipile.example/v2/acc_forme/messages/m1/attachments/a1"
    msgs = [{"id": "m1", "attachments": [{"id": "a1", "type": "audio", "url": url}]}]
    out = M.references_des_pieces(M.page_de_messages(_page(msgs)), "WHATSAPP")
    assert out["items"][0]["attachments"][0]["source"] == {
        "kind": "unipile", "channel": "whatsapp", "url": url}
    assert "oto_import" in out["attachments_note"]
    assert "attachments_without_url" not in out


def test_une_piece_sans_url_est_dite_irrecuperable():
    msgs = [{"id": "m1", "attachments": [{"id": "a1", "type": "img"},
                                         {"id": "a2", "url": "http://x.example/a2"}]}]
    out = M.references_des_pieces(_page(msgs), "telegram")
    assert all("source" not in a for a in out["items"][0]["attachments"])
    assert out["attachments_without_url"].startswith("2 attachment(s)")
    assert "attachments_note" not in out


def test_une_page_linkedin_garde_sa_forme_et_gagne_les_references():
    """LinkedIn sert sa page brute (`data` ET `items`, même liste) : seule la référence
    s'ajoute, une seule fois par pièce."""
    url = "https://unipile.example/v2/acc_forme/messages/m1/attachments/a1"
    msgs = [{"id": "m1", "attachments": [{"url": url}]}]
    out = M.references_des_pieces(_page(msgs), "linkedin")
    assert out["data"] is out["items"]
    assert out["data"][0]["attachments"][0]["source"]["channel"] == "linkedin"


def test_une_page_qui_n_est_pas_un_objet_passe_telle_quelle():
    assert M.references_des_pieces(None, "whatsapp") is None
    assert M.references_des_pieces({"items": "x"}, "whatsapp") == {"items": "x"}


# --- l'outil branché (le même pour les trois canaux) ---------------------------

def _outil(monkeypatch, canal="WHATSAPP"):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    client = MagicMock()
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: client)
    m = FastMCP("t")
    U.register_messaging_tools(m, canal)
    return asyncio.run(m.get_tool(f"{canal.lower()}_chat")).fn, client


def test_l_outil_sert_la_page_remise_en_forme(monkeypatch):
    fn, client = _outil(monkeypatch)
    client.list_chats.return_value = _page([{"id": "c1"}, {"id": "c1"}])
    out = fn(op="list", limit=5)
    assert out["items"] == [{"id": "c1"}] and "data" not in out
    assert client.list_chats.call_args.kwargs["limit"] == 5


def test_l_outil_refuse_une_page_trop_grande_sans_appeler_unipile(monkeypatch):
    fn, client = _outil(monkeypatch)
    with pytest.raises(McpError, match="1 to 100"):
        fn(op="list", limit=200)
    client.list_chats.assert_not_called()


def test_la_lecture_passe_par_la_remise_en_forme(monkeypatch):
    fn, client = _outil(monkeypatch, "TELEGRAM")
    client.list_messages.return_value = _page([{"id": "m1", "text": ""}])
    out = fn(op="read", chat_id="c1")
    assert out["items"][0]["content_unavailable"] is True


def test_la_lecture_pose_la_reference_du_canal_lu(monkeypatch):
    fn, client = _outil(monkeypatch, "INSTAGRAM")
    url = "https://unipile.example/v2/acc_forme/messages/m1/attachments/a1"
    client.list_messages.return_value = _page([{"id": "m1", "attachments": [{"url": url}]}])
    out = fn(op="read", chat_id="c1")
    assert out["items"][0]["attachments"][0]["source"] == {
        "kind": "unipile", "channel": "instagram", "url": url}


# --- une panne amont dit ce qu'elle dit (1354, 1359) ---------------------------

class _Amont(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


def test_un_5xx_porte_le_message_de_l_amont():
    from oto_mcp.error_taxonomy import classify
    info = classify(_Amont(500, "Unipile 500: account disconnected, reconnect it"))
    assert info.code == "upstream_5xx" and info.retryable is True
    assert "account disconnected, reconnect it" in info.message


def test_un_5xx_qui_est_une_page_html_n_en_rend_rien():
    from oto_mcp.error_taxonomy import classify
    info = classify(_Amont(502, "<!DOCTYPE html><html><body>Bad gateway</body></html>"))
    assert info.message == "The upstream service hit an error (502)."


def test_un_5xx_bavard_est_borne():
    from oto_mcp.error_taxonomy import classify
    info = classify(_Amont(500, "panne amont " * 200))
    assert len(info.message) < 400 and info.message.endswith("…")
