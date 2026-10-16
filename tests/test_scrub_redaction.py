"""L'action `scrub` : les valeurs sensibles écrites DANS le texte des réponses.

Une règle `scrub` cherche, dans les chaînes, des valeurs par leur forme (oto-core, en
local) ou par un modèle que l'instance déclare (`scrub_model`). Ce que ce fichier tient :
la pose refuse ce que le serveur ne saurait pas faire, le filtre ne construit le modèle
que pour une règle qui le demande, le texte libre est nettoyé comme le JSON, et la
rédaction ne tourne jamais dans la boucle.
"""
import asyncio
import json
import threading

import pytest
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from oto.tools.common import FieldFilter
from oto.tools.common.scrub import ModelDetectionError

from oto_mcp import field_filter_defaults, redaction, scrub_model
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.orgs import field_filters as ff
from oto_mcp.middleware import field_redaction

NIR = "1 85 05 78 006 084 91"
PARTOUT = [{"fields": ["*"], "action": "scrub", "detect": ["nir", "secret"]}]
MODELE = {"OTO_SCRUB_MODEL_URL": "https://modele.exemple/v1", "OTO_SCRUB_MODEL": "m",
          "OTO_SCRUB_MODEL_KEY": "k"}


@pytest.fixture
def sans_modele(monkeypatch):
    for nom in MODELE:
        monkeypatch.delenv(nom, raising=False)


@pytest.fixture
def avec_modele(monkeypatch):
    for nom, valeur in MODELE.items():
        monkeypatch.setenv(nom, valeur)


@pytest.fixture
def ecrit(monkeypatch):
    monkeypatch.setattr(ff.org_store, "get_org", lambda org_id: {"id": org_id})
    monkeypatch.setattr(ff.org_store, "set_org_field_filters", lambda *a: None)

    def _ecrire(rules):
        return ff._set_field_filter(None, ff.SetFieldFilterInput(
            org_id=1, service="zohodesk", rules=rules))

    return _ecrire


def _refus(fn, code):
    with pytest.raises(AuthzDenied) as exc:
        fn()
    assert exc.value.code == code
    return exc.value


# --- la pose ------------------------------------------------------------------------

@pytest.mark.parametrize("regle,code", [
    ({"fields": ["*"], "action": "scrub"}, "scrub_without_detect"),
    ({"fields": ["*"], "action": "scrub", "detect": []}, "scrub_without_detect"),
    ({"fields": ["*"], "action": "scrub", "detect": ["nir", "religion"]}, "unknown_scrub_kind"),
])
def test_une_regle_scrub_mal_formee_est_refusee(ecrit, sans_modele, regle, code):
    _refus(lambda: ecrit([regle]), code)


def test_les_formes_se_posent_sans_modele(ecrit, sans_modele):
    assert ecrit(PARTOUT)["ok"]


def test_une_sorte_de_modele_sans_modele_est_refusee_a_la_pose(ecrit, sans_modele):
    refus = _refus(lambda: ecrit([{"fields": ["*"], "action": "scrub",
                                   "detect": ["nir", "person"]}]), "scrub_model_unconfigured")
    assert refus.status == 409 and "OTO_SCRUB_MODEL_URL" in str(refus.message)


def test_le_template_texte_libre_se_pose_avec_le_modele(ecrit, avec_modele):
    assert ecrit(field_filter_defaults.TEMPLATES["free_text"]["rules"])["ok"]


def test_le_formulaire_connait_scrub():
    (scrub,) = [a for a in ff._ACTION_SCHEMA if a["action"] == "scrub"]
    from oto.tools.common.scrub import KINDS

    assert scrub["params"][0]["options"] == list(KINDS)


# --- le filtre ----------------------------------------------------------------------

def test_le_modele_n_est_construit_que_pour_une_regle_qui_le_demande(avec_modele):
    assert field_filter_defaults.filtre({"rules": PARTOUT}).detector is None
    avec = field_filter_defaults.filtre({"rules": [{"fields": ["*"], "action": "scrub",
                                                    "detect": ["person"]}]})
    assert avec.detector is not None and avec.detector.model == "m"


def test_un_modele_declare_a_moitie_leve(monkeypatch, sans_modele):
    monkeypatch.setenv("OTO_SCRUB_MODEL_URL", "https://modele.exemple/v1")
    with pytest.raises(RuntimeError, match="OTO_SCRUB_MODEL_KEY"):
        scrub_model.declare()
    # …mais ne retient que les politiques qui s'en servent.
    assert field_filter_defaults.filtre({"rules": PARTOUT}).detector is None


# --- le résultat servi ----------------------------------------------------------------

class _Ctx:
    def __init__(self, name):
        self.message = type("M", (), {"name": name})()


def _servir(result, filtre, monkeypatch, fils=None):
    def _resolve(_service):
        if fils is not None:
            fils.append(threading.current_thread() is threading.main_thread())
        return filtre

    monkeypatch.setattr(redaction, "_resolve_field_filter", _resolve)

    async def call_next(_ctx):
        return result

    return asyncio.run(field_redaction.FieldRedactionMiddleware().on_call_tool(
        _Ctx("zohodesk_ticket_threads"), call_next))


def test_le_json_est_nettoye_sur_les_deux_canaux(monkeypatch):
    payload = {"data": [{"content": f"<p>mdp : Toto123! NIR {NIR}</p>"}]}
    out = _servir(ToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                             structured_content=payload), FieldFilter(PARTOUT), monkeypatch)
    for vue in (out.structured_content, json.loads(out.content[0].text)):
        texte = vue["data"][0]["content"]
        assert "Toto123!" not in texte and NIR not in texte and "[NIR-" in texte


def test_le_texte_libre_est_nettoye_aussi(monkeypatch):
    image = ImageContent(type="image", data="AAAA", mimeType="image/png")
    brut = ToolResult(content=[TextContent(type="text", text=f"Client : NIR {NIR}."), image,
                               TextContent(type="text", text="rien")])
    out = _servir(brut, FieldFilter(PARTOUT), monkeypatch)
    assert out.content[0].text.startswith("Client : NIR [NIR-") and out.content[1] is image
    assert out.content[2].text == "rien" and not out.is_error


def test_le_texte_libre_sans_scrub_partout_reste_tel_quel(monkeypatch):
    brut = ToolResult(content=[TextContent(type="text", text=f"NIR {NIR}")])
    regles = [{"fields": ["content"], "action": "scrub", "detect": ["nir"]},
              {"fields": ["iban"], "action": "mask"}]
    assert _servir(brut, FieldFilter(regles), monkeypatch) is brut


def test_un_modele_en_echec_retient_la_sortie(monkeypatch):
    class _Panne:
        def find(self, texts, kinds):
            raise ModelDetectionError("model endpoint answered HTTP 503")

    filtre = FieldFilter([{"fields": ["*"], "action": "scrub", "detect": ["person"]}],
                         detector=_Panne())
    payload = {"content": "Ticket de Martin Durand"}
    out = _servir(ToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                             structured_content=payload), filtre, monkeypatch)
    assert out.is_error and "Martin" not in out.content[0].text


def test_la_redaction_ne_tourne_pas_dans_la_boucle(monkeypatch):
    fils = []
    _servir(ToolResult(content=[TextContent(type="text", text="{}")], structured_content={}),
            FieldFilter(PARTOUT), monkeypatch, fils)
    assert fils == [False]


# --- l'essai à blanc -------------------------------------------------------------------

def test_l_essai_a_blanc_dit_la_panne_du_modele(monkeypatch, avec_modele):
    monkeypatch.setattr(ff.org_store, "get_org", lambda org_id: {"id": org_id})

    def _panne(self, texts, kinds):
        raise ModelDetectionError("model endpoint unreachable: ConnectTimeout")

    monkeypatch.setattr("oto.tools.common.scrub.ChatSpanDetector.find", _panne)
    _refus(lambda: ff._preview_field_filter(None, ff.PreviewFieldFilterInput(
        org_id=1, service="zohodesk", payload={"content": "Martin Durand"},
        rules=[{"fields": ["*"], "action": "scrub", "detect": ["person"]}])),
        "scrub_model_failed")
