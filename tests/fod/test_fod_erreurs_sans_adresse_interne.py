"""Ce qu'un échec FOD SERT à l'utilisateur : une cause nommée, jamais notre réseau.

Mesuré en production le 2026-09-09 sur les trois lectures DVF+ : l'appel brûlait
~60 s puis rendait

    Server error '502 Bad Gateway' for url 'http://<ip-interne>:8000/api/foncier/…'

soit deux défauts dans une seule chaîne — une adresse RFC 1918 de notre réseau servie
telle quelle, et une panne lente qui ne dit pas ce qui a lâché. Le message venait de
`httpx.Response.raise_for_status()`, qui met l'URL ABSOLUE dans son texte ; le délai,
de l'attente jusqu'au timeout de la passerelle.

Chaque test de non-divulgation est doublé de son CONTRÔLE POSITIF : prouver d'abord
que l'adresse EST là sans la garde, sinon « elle n'apparaît pas » ne mesure rien.

L'adresse jouée ici est une RFC 1918 quelconque, PAS la nôtre : ce dépôt est public,
et un banc qui prouve qu'on ne divulgue pas son réseau ne le divulgue pas non plus.
"""
import httpx
import pytest

from oto_mcp.fod import http as fod_http

_HOTE_INTERNE = "http://10.42.0.7:8000"
_CHEMIN = "/api/foncier/dvf/stats"


def _reponse(status: int, body: bytes = b"", base: str = _HOTE_INTERNE) -> httpx.Response:
    req = httpx.Request("POST", base + _CHEMIN)
    return httpx.Response(status, request=req, content=body)


# --- contrôle positif : sans garde, l'adresse interne EST servie --------------

def test_controle_positif_httpx_met_bien_l_hote_interne_dans_son_message():
    """Si ce test tombait, les suivants ne prouveraient plus rien : ils vérifieraient
    l'absence d'une chaîne que rien ne produit."""
    with pytest.raises(httpx.HTTPStatusError) as e:
        _reponse(502).raise_for_status()
    assert "10.42.0.7:8000" in str(e.value)


# --- la garde ----------------------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 504, 404, 418])
def test_aucun_echec_ne_nomme_une_adresse_de_notre_reseau(status):
    with pytest.raises(RuntimeError) as e:
        fod_http._raise_for(_reponse(status))
    assert "10.42.0" not in str(e.value)


def test_le_502_nomme_la_source_amont_et_le_chemin():
    """502 = FOD a répondu, mais l'API publique qu'il interroge s'est tue. Le dire
    évite de lire « pas de mutation ici » là où il faut lire « source muette »."""
    with pytest.raises(RuntimeError) as e:
        fod_http._raise_for(_reponse(502))
    msg = str(e.value)
    assert "Source amont indisponible" in msg
    assert _CHEMIN in msg          # le CHEMIN reste, il est actionnable
    assert "10.42.0.7" not in msg
    assert "8000" not in msg


def test_le_detail_relaye_par_le_service_est_lui_aussi_lave():
    """FOD peut relayer, dans son `detail`, un message httpx interne portant l'hôte :
    laver seulement notre propre texte laisserait passer celui du service."""
    corps = b'{"detail": "Server error for url \'http://10.0.0.7:8000/api/foncier/x\'"}'
    with pytest.raises(RuntimeError) as e:
        fod_http._raise_for(_reponse(500, corps))
    assert "10.0.0.7" not in str(e.value)


def test_l_hote_configure_est_lave_meme_sous_forme_de_nom(monkeypatch):
    """`FOD_BASE_URL` n'est pas forcément une IP : un nom d'hôte interne se sert
    aussi peu."""
    monkeypatch.setattr(fod_http, "_BASE", "http://fod-0.interne:8000")
    corps = b'{"detail": "upstream http://fod-0.interne:8000 refused"}'
    with pytest.raises(RuntimeError) as e:
        fod_http._raise_for(_reponse(500, corps, base="http://fod-0.interne:8000"))
    assert "fod-0.interne" not in str(e.value)


# --- échouer VITE, et en nommant la cause ------------------------------------

def test_une_lecture_bornee_refuse_sans_attendre_la_passerelle(monkeypatch):
    """Sans borne à nous, l'appel attendait le timeout de la passerelle (~60 s) pour
    rendre un 502 muet. Une lecture dont l'amont est tombé doit refuser vite, en
    nommant ce qui a lâché — et sans réessayer, un amont muet ne guérit pas en 0,5 s.
    """
    appels = []

    class _Faux:
        def request(self, method, path, **kw):
            appels.append(kw.get("timeout"))
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(fod_http, "_c", lambda: _Faux())
    with pytest.raises(RuntimeError) as e:
        fod_http.post(_CHEMIN, {"code_commune": "75101"}, timeout=20.0)
    msg = str(e.value)
    assert "Source amont indisponible" in msg and "20 s" in msg
    assert "10.42.0" not in msg
    # UN seul appel : le retry borné vise la saturation (503/429), pas un amont mort.
    assert len(appels) == 1
    assert appels[0].read == 20.0


def test_les_lectures_dvf_portent_la_borne(monkeypatch):
    """Les trois lectures DVF+ tapent l'API Cerema `apidf-preprod` — le seul amont
    dont on a mesuré qu'il tombe. Ce sont elles qui doivent être bornées."""
    from oto_mcp.fod import foncier as fod_foncier

    vus = []
    monkeypatch.setattr(fod_foncier, "_post",
                        lambda path, body, timeout=None: vus.append((path, timeout)))
    fod_foncier.dvf.stats(code_commune="75101")
    fod_foncier.dvf.comparables(code_commune="75101")
    fod_foncier.dvf.comparables_by_address(adresse="1 rue de la Paix")
    assert [t for _, t in vus] == [fod_foncier._DVF_TIMEOUT_S] * 3
    assert all(t and t < 60 for _, t in vus)
