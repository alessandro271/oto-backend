"""Transport épinglé — la garde d'egress appliquée À LA CONNEXION, pas avant.

`egress.check_url` contrôle une URL puis rend la main ; le client HTTP résout
ensuite l'hôte UNE SECONDE FOIS pour se connecter. Entre les deux, un DNS hostile
peut changer d'avis (DNS rebinding), et un 3xx peut désigner un tout autre hôte
que personne n'a contrôlé. Ce module ferme ces deux fenêtres pour toute session
`requests` qui le monte :

- à chaque ouverture de socket — premier appel, connexion de reprise, CHAQUE saut
  de redirection — l'hôte est résolu une seule fois par `egress.resolved_addresses`
  (la couture que les épreuves remplacent) ;
- la MÊME décision que `check_url` s'applique à ces adresses (`egress.refuse_instance`
  puis `egress.decide` : toute adresse interne refuse, exceptions nommées selon la
  politique) ;
- la socket s'ouvre sur l'une de CES adresses littérales, essayées dans l'ordre
  (IPv4 d'abord) — il n'y a plus de seconde résolution ;
- la connexion garde son nom d'hôte d'origine : SNI, vérification du certificat
  contre ce nom et en-tête `Host` sont ceux d'urllib3, inchangés. On n'épingle que
  l'adresse, jamais l'identité.

**Pas de patch global** de `getaddrinfo` ni de `create_connection` : le processus
parle légitimement à sa boucle locale (base, services de la machine). Seules les
sessions gardées sont concernées, une politique par session.

**Les proxies d'environnement sont ignorés** (`trust_env=False`) et un proxy
explicite est REFUSÉ : derrière un proxy, c'est lui qui résout, et l'épinglage ne
voit plus rien. Le dépôt ne lit aucune variable de proxy (`env_inventory.py`) ; une
instance qui ferait sortir son trafic par un proxy d'egress voit ces chemins-ci
ÉCHOUER — fermé, jamais ouvert. `.netrc` est écarté aussi.

**Le magasin de certificats de l'environnement, lui, est repris** : `trust_env=False`
l'écartait avec le reste, et un connecteur installé chez un client derrière une
autorité de certification privée ne vérifiait plus son serveur. `harden` relit donc
`REQUESTS_CA_BUNDLE` puis `CURL_CA_BUNDLE`, dans l'ordre où `requests` les lit, et
les pose en `verify` de la session. Cela ne touche que la VÉRIFICATION TLS : aucune
destination n'est ajoutée, la décision sur l'adresse reste la même. Sans elles, le
magasin par défaut (`certifi`).

**Chaque session dit au nom de qui elle sort** (`titulaire`, OBLIGATOIRE) : la
décision à la connexion est celle de `egress.check_url` pour le MÊME titulaire —
les seules exceptions nommées qui le désignent (`egress.declared_exceptions`), aucune
pour une URL choisie par un agent (`egress.AGENT`) ni pour personne
(`egress.AUCUN`). C'est ce qui ferme le rebinding : la résolution qui fait foi est
celle de la connexion, jugée avec la même règle.

Le refus à la connexion lève `egress.EgressRefused` — un `ValueError`, que ni
urllib3 ni requests n'attrapent : il traverse jusqu'à l'appelant, comme le refus
précoce. Chaque site câblé le traduit pour sa face. `check_url` reste appelé en
amont : il tombe avant qu'un client soit construit et nomme la carte à corriger ;
ce transport-ci est le point qui FAIT FOI.
"""
from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (ConnectTimeoutError, NameResolutionError,
                                NewConnectionError)
from urllib3.util import connection as _connexion

from . import egress


@dataclass(frozen=True)
class Politique:
    """Ce que la garde dit dans son refus (`connector`, `field`) et au nom de qui on
    sort (`titulaire`, `egress.titulaire_de`, `egress.PLATEFORME`, `egress.AUCUN` ou
    `egress.AGENT`) : seules les exceptions nommées qui le désignent s'appliquent."""
    connector: str
    field: str
    titulaire: tuple


def _ouvrir(adresse: str, port: int, timeout, source_address, socket_options):
    """Ouvre la socket vers une adresse LITTÉRALE. Couture des épreuves : elles la
    remplacent pour voir l'adresse jointe sans toucher au réseau."""
    return _connexion.create_connection((adresse, port), timeout,
                                        source_address=source_address,
                                        socket_options=socket_options)


def _ordre(adresses) -> list[str]:
    """IPv4 d'abord (un hôte double pile dont l'IPv6 n'est pas routable ne doit pas
    échouer), puis un ordre stable."""
    return sorted(adresses, key=lambda a: (":" in a, a))


class _Epinglee:
    """Mixin de connexion urllib3 : remplace la résolution + connexion de
    `_new_conn` par résolution UNIQUE → décision → connexion à l'adresse décidée.
    `host` n'est pas touché : TLS et `Host` restent ceux du nom d'origine."""

    _politique: Politique

    def _new_conn(self) -> socket.socket:
        p = self._politique
        hote = self.host
        egress.refuse_instance(hote, connector=p.connector, field=p.field)
        try:
            adresses = egress.resolved_addresses(self._dns_host, self.port)
        except socket.gaierror as e:
            raise NameResolutionError(hote, self, e) from e
        except OSError as e:
            raise NewConnectionError(
                self, f"Failed to resolve {hote}: {e}") from e
        if not adresses:
            raise NameResolutionError(hote, self, socket.gaierror(
                socket.EAI_NONAME, "no address"))
        egress.decide(hote, self.port, adresses, connector=p.connector,
                      field=p.field, titulaire=p.titulaire)

        derniere: Exception | None = None
        for adresse in _ordre(adresses):
            try:
                sock = _ouvrir(adresse, self.port, self.timeout,
                               self.source_address, self.socket_options)
            except socket.timeout as e:
                derniere = ConnectTimeoutError(
                    self, f"Connection to {hote} timed out. "
                          f"(connect timeout={self.timeout})")
                derniere.__cause__ = e
            except OSError as e:
                derniere = NewConnectionError(
                    self, f"Failed to establish a new connection: {e}")
                derniere.__cause__ = e
            else:
                sys.audit("http.client.connect", self, hote, self.port)
                return sock
        raise derniere  # type: ignore[misc]


@lru_cache(maxsize=4096)
def _classes_de_pool(politique: Politique) -> dict:
    """Les classes de pool d'une politique — la politique vit SUR les classes,
    jamais dans les `pool_kwargs` (qui entrent dans la clé de pool d'urllib3).

    Mémorisées par politique (`Politique` est figée, donc hachable) : un client par
    appel (`tools/api.client_for`, une session par lecture d'URL) ne refabrique pas
    quatre classes à chaque fois. Le titulaire porte l'id d'une entité : les
    politiques suivent le nombre d'équipes, d'orgs et de membres servis, d'où un cache
    BORNÉ (une politique évincée se refabrique, à l'identique). Le dict rendu est
    partagé : on le recopie avant de le poser sur un poolmanager."""
    class ConnexionHttp(_Epinglee, HTTPConnection):
        _politique = politique

    class ConnexionHttps(_Epinglee, HTTPSConnection):
        _politique = politique

    class PoolHttp(HTTPConnectionPool):
        ConnectionCls = ConnexionHttp

    class PoolHttps(HTTPSConnectionPool):
        ConnectionCls = ConnexionHttps

    return {"http": PoolHttp, "https": PoolHttps}


class AdaptateurEpingle(HTTPAdapter):
    """L'adaptateur `requests` qui monte les connexions épinglées d'une politique."""

    def __init__(self, politique: Politique, **kw):
        self._politique = politique
        self._pools = _classes_de_pool(politique)
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        super().init_poolmanager(*a, **kw)
        self.poolmanager.pool_classes_by_scheme = dict(self._pools)

    def __setstate__(self, state):
        # Un adaptateur re-créé par pickle reconstruit son poolmanager : sans
        # ceci il le referait avec les classes d'urllib3, non épinglées.
        self._pools = _classes_de_pool(state["_politique"])
        super().__setstate__(state)

    __attrs__ = HTTPAdapter.__attrs__ + ["_politique"]

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        p = self._politique
        raise egress.EgressRefused(
            f"`{p.connector}` : un proxy ({proxy}) est refusé sur une session gardée — "
            "derrière un proxy, c'est lui qui résout l'hôte, et la garde d'egress "
            f"ne verrait plus l'adresse jointe. (Champ `{p.field}`.)")


def _magasin_ca() -> str | None:
    """Le magasin de certificats posé par l'environnement, lu comme `requests` le lit
    quand il fait confiance à l'environnement (`REQUESTS_CA_BUNDLE`, puis
    `CURL_CA_BUNDLE`)."""
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or None


def harden(s: requests.Session, *, connector: str, field: str = "base_url",
           titulaire: tuple) -> requests.Session:
    """Monte le transport épinglé sur `s` (http:// et https://), coupe les proxies
    d'environnement et reprend son magasin de certificats (`_magasin_ca`). Rend `s`.
    `titulaire` OBLIGATOIRE : au nom de qui la session sort (`Politique`)."""
    adaptateur = AdaptateurEpingle(Politique(connector, field,
                                             egress._valider(titulaire)))
    s.mount("http://", adaptateur)
    s.mount("https://", adaptateur)
    s.trust_env = False
    ca = _magasin_ca()
    if ca and s.verify is True:
        s.verify = ca
    return s


def session(*, connector: str, field: str = "url",
            titulaire: tuple) -> requests.Session:
    """Une session `requests` neuve, gardée (`harden`), au nom de `titulaire`."""
    return harden(requests.Session(), connector=connector, field=field,
                  titulaire=titulaire)


def pinned(client, *, connector: str, field: str = "base_url", titulaire: tuple):
    """Un client d'oto-core qui porte sa `requests.Session` en `.session`, rendu avec
    cette session gardée au nom de `titulaire`. Rend `client`.
    ⚠️ Ne couvre que ce qui passe par `.session` : un appel `requests.<verbe>` au
    niveau module dans la lib reste hors du transport (`docs/connector-vault.md`)."""
    harden(client.session, connector=connector, field=field, titulaire=titulaire)
    return client
