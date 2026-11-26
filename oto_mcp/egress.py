"""Garde d'egress des connecteurs — une destination de credential ne vise pas l'intérieur.

Sept connecteurs prennent leur hôte dans le credential : `api` (`base_url` et
`token_url`), `n8n`, `make`, `github` Enterprise, `lighton`, `posthog`,
`salesforce`. Cette valeur est déclarée par un **administrateur d'organisation
cliente**, et le process qui l'appelle tourne en root, sans conteneur : sa boucle
locale porte l'administration du proxy web, le pilotage du navigateur et le reste
des services de la machine. Sans garde, une `base_url` suffit à faire parler le
serveur à lui-même.

Le filtrage réseau du service ne couvre qu'**une** plage (le lien-local, donc les
métadonnées d'infrastructure) ; les plages privées et la boucle locale restaient
joignables. La garde vit donc dans le code, ici.

⚠️ **Elle contredit l'ADR 0037 §4**, qui décidait « zéro code SSRF dans le
connecteur » et renvoyait le blocage des plages privées à un egress proxy
« plus tard… si le besoin naît ». Ce proxy n'a jamais existé, et l'argument qui
justifiait l'absence de garde reposait sur lui. La décision a été reprise ;
l'ADR est à amender.

**Le refus se fait à la RÉSOLUTION, pas sur la forme du texte.** Un nom de
domaine parfaitement public peut pointer vers l'intérieur, et une adresse peut
s'écrire autrement qu'en quatre octets pointés (`127.1`, `2130706433`,
`[::ffff:127.0.0.1]`). Toutes ces formes passent un contrôle textuel ; aucune ne
passe `getaddrinfo`. Fail-closed sur l'ENSEMBLE des adresses résolues : un hôte
qui résout à la fois public et interne est refusé — c'est le montage type du
contournement.

**Les exceptions sont NOMMÉES et déclarées**, jamais devinées : une destination
interne légitime (un pont hébergé en boucle locale, un service sur le réseau
privé) doit figurer dans la variable d'environnement `OTO_EGRESS_ALLOW`, au
format `nom=adresse:port[@palier:id[,palier:id…]]`, séparé par des virgules ou des
retours à la ligne. La clé est une **adresse littérale et un port** — jamais un nom
d'hôte : on autorise une destination, pas une étiquette qui peut se déplacer sous
nous ; et jamais une adresse seule, sinon une exception pour un pont ouvrirait toute
la machine.

**Chaque exception a son TITULAIRE.** Le suffixe `@palier:id` (`group`, `org` ou
`tenant`, et l'identifiant de l'entité) nomme les identifiants qui y ont
droit : celui dont la ligne du coffre a EXACTEMENT ce palier et cet id — une équipe
n'hérite pas de son org ni l'inverse, un membre n'hérite de rien. Une exception
SANS suffixe ne vaut que pour un identifiant du palier plateforme, posé par
l'opérateur. Plusieurs titulaires se séparent par des virgules
(`pont=127.0.0.1:9000@group:3,org:7`) : un morceau sans `=` qui suit une entrée à
suffixe est un titulaire de plus. Une entrée mal formée refuse le démarrage
(`verifier_reglages`). Une entrée sans suffixe — le format d'avant — se lit
toujours, et vaut pour la plateforme seule.

**Chaque appel dit au nom de qui il sort.** `titulaire` est un paramètre
OBLIGATOIRE de `check_url`, `decide` et des sessions d'`egress_transport` : la
ligne du coffre de l'identifiant servi (`titulaire_de`, `titulaire_d_instance`,
`titulaire_des_champs`), `PLATEFORME`, `AUCUN` (pas encore de ligne : aucune
exception) ou `AGENT` (une URL choisie par un agent : aucune exception, et le refus
le dit à l'agent). Un appel qui ne le dit pas ne s'exécute pas.

Le défaut est **vide** : ce fichier est public, les destinations réelles se
déclarent au déploiement. Une destination absente de la liste échoue avec un
message qui nomme la variable et le format — jamais en silence.

**Le contrôle fait foi à la CONNEXION (`egress_transport`).** `check_url` est le
refus précoce, celui qui explique : il tombe avant qu'un client soit construit et
nomme la carte à corriger. Mais entre ce contrôle et la connexion, le client HTTP
résolvait une seconde fois — un DNS hostile qui répond « public » puis « interne »
(DNS rebinding) passait, et une redirection 3xx vers l'intérieur passait aussi,
puisque la garde tenait là où le backend construit le client, pas dans son
transport. Les sessions gardées (`egress_transport.session` / `.harden`) ferment
ces deux fenêtres : à chaque connexion, redirections comprises, l'hôte est résolu
UNE fois, la décision ci-dessous (`decide`) s'applique à ces adresses-là, et la
socket s'ouvre sur l'une d'elles — le nom d'origine restant celui du SNI, de la
vérification du certificat et de l'en-tête `Host`. ⚠️ Ne couvre que les chemins qui
passent par une session gardée : un client de la lib qui appelle `requests.<verbe>`
au niveau module n'en a pas (`docs/connector-vault.md` §Egress, la liste).

**L'instance elle-même n'est pas une destination (#1499).** Son hôte public
(`config.public_host()`, lu de `OTO_MCP_PUBLIC_URL`), ses sous-domaines et ses hôtes
épinglés par org (`<slug>--<hôte>`, `subdomain_org`) sont refusés, quelle que soit la
politique et sans exception possible : un agent n'appelle pas oto par un connecteur.
Une carte `api` dont la `base_url` visait l'instance joignait `/api/hooks/…` avec un
porteur obtenu ailleurs, et réveillait un agent hors de toute liste d'outils. Sans la
variable, la garde LÈVE (comme `config.public_host`) : on ne devine pas quel hôte est
le nôtre. ⚠️ Limite : un AUTRE nom du même service (l'adresse IP de la machine, un
domaine de tenant) n'est pas reconnu — comparer les adresses résolues refuserait tout
site qui partage un frontal avec nous.

Deux gardes plus anciennes et plus étroites font un travail voisin sans être
fusionnées ici : `tools/web.py:check_url_public` et
`file_source._assert_public_host`. Elles appliquent une politique DIFFÉRENTE —
URL fournie par l'agent, aucune exception possible, tout ce qui n'est pas public
est refusé. Ici l'URL vient d'un administrateur d'organisation et des exceptions
existent. Les rapprocher supposerait de trancher ces deux politiques ; ce n'est
pas ce lot.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Variable d'environnement qui porte les exceptions nommées.
ALLOW_VAR = "OTO_EGRESS_ALLOW"

#: Le titulaire d'une exception, ou celui au nom duquel on sort : `(palier, id)`.
Titulaire = tuple[str, str]
#: Un identifiant du palier plateforme (posé par l'opérateur) : les exceptions sans
#: suffixe `@`. Le premier membre vaut `credentials_store.PLATFORM`.
PLATEFORME: Titulaire = ("platform", "")
#: Personne : aucune exception (une clé sans ligne du coffre, en cours de pose).
AUCUN: Titulaire = ("", "")
#: Une URL CHOISIE PAR UN AGENT (lecture de page, source `url`, document public) :
#: aucune exception, et le refus dit le geste de ce lecteur-là.
AGENT: Titulaire = ("agent", "")
#: Les paliers qu'un suffixe `@palier:id` peut nommer. Pas le membre : la clé
#: personnelle d'un membre n'hérite d'aucune exception.
PALIERS_TITULAIRES = ("group", "org", "tenant")


def titulaire_d_instance(instance) -> Titulaire:
    """Le titulaire de la ligne qu'une sonde éprouve (`instance` =
    `(entity_type, entity_id, account)`), `AUCUN` sans ligne (une clé en cours de
    pose)."""
    return titulaire_de(*(instance or (None, None))[:2])


def titulaire_des_champs(champs) -> Titulaire:
    """Le titulaire que portent des champs résolus (`access.ChampsResolus`, rendus par
    `access.resolve_credential_fields`) ; `AUCUN` pour un dict nu."""
    return _valider(getattr(champs, "titulaire", AUCUN))


def titulaire_de(entity_type: Optional[str], entity_id) -> Titulaire:
    """Le titulaire d'un identifiant d'après sa ligne du coffre
    (`ResolvedCredential.entity_type`/`entity_id`, `instance` d'une sonde) : la
    plateforme, son palier et son id, ou personne quand l'origine est inconnue. Une
    clé de membre rend son propre couple, qu'aucune exception ne peut nommer."""
    if entity_type == PLATEFORME[0]:
        return PLATEFORME
    if entity_type and entity_id not in (None, ""):
        return (str(entity_type), str(entity_id))
    return AUCUN


class EgressRefused(ValueError):
    """Une destination de credential vise une adresse interne non déclarée.

    Hérite de `ValueError` : les adaptateurs de connecteurs traduisent déjà un
    `ValueError` de config en refus actionnable pour l'appelant."""


# ── ce qui est « interne » ────────────────────────────────────────────────────

def _unwrap(ip):
    """Déballe les encapsulations v4-dans-v6 (`::ffff:127.0.0.1`, 6to4, Teredo).

    ⚠️ **Ne change pas la DÉCISION** — mesuré : `is_global` est déjà faux pour
    ces cinq formes, elles sont refusées avec ou sans ce déballage. Ce qu'il
    change est la RAISON servie : sans lui, un 6to4 qui joint la boucle locale
    (`2002:7f00:0001::`) est annoncé « réseau privé », et l'opérateur cherche la
    mauvaise chose. Le garder pour la précision du refus, pas croire qu'il ferme
    une porte."""
    if isinstance(ip, ipaddress.IPv6Address):
        for encapsule in (ip.ipv4_mapped, ip.sixtofour):
            if encapsule is not None:
                return encapsule
        if ip.teredo is not None:
            return ip.teredo[1]
    return ip


def internal_reason(adresse: str) -> str | None:
    """Pourquoi cette adresse est interne, en clair — ou `None` si elle est publique.

    La décision est le OU de TOUTES les branches ci-dessous, `not is_global`
    compris en dernier ressort : une plage spéciale qu'aucune branche ne nomme est
    refusée quand même, avec une raison générique. L'ordre ne sert qu'à choisir le
    mot juste — un refus qui ne nomme pas sa raison se fait contourner au lieu
    d'être corrigé.

    ⚠️ **Ne pas réduire ce corps à `not is_global`.** La box et la CI tournent
    sous Python 3.10, le banc sous 3.13, et les prédicats d'`ipaddress` ont bougé
    entre ces versions sur les formes encapsulées. Vérification faite sur les 20
    adresses du banc, **les deux versions décident et motivent identiquement** —
    donc rien n'est corrigé ici, c'est une précaution : la décision ne repose pas
    sur un prédicat unique dont la définition a déjà changé une fois."""
    ip = _unwrap(ipaddress.ip_address(adresse))
    if ip.is_unspecified:
        return "adresse non spécifiée — elle désigne la machine elle-même"
    if ip.is_loopback:
        return "boucle locale de la machine"
    if ip.is_link_local:
        return "lien-local — c'est la plage des métadonnées d'infrastructure"
    if ip.is_private:
        return "réseau privé (RFC 1918 en IPv4, ULA en IPv6)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "plage réservée"
    if not ip.is_global:
        return "adresse non publique (plage à usage spécial)"
    return None


# ── les exceptions nommées ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Exception_:
    """Une exception déclarée : `titulaires` vide = la plateforme seulement."""
    nom: str
    adresse: str
    port: int
    titulaires: frozenset


_FORMAT = ("« nom=adresse:port[@palier:id[,palier:id…]] », p. ex. "
           "« pont-local=127.0.0.1:9000 » (plateforme) ou "
           "« pont-local=127.0.0.1:9000@group:3 »")


def _titulaire(morceau: str, nom: str) -> Titulaire:
    palier, _, ident = morceau.strip().partition(":")
    palier, ident = palier.strip(), ident.strip()
    if (palier not in PALIERS_TITULAIRES or not ident
            or any(c.isspace() or c in "@=:" for c in ident)):
        raise ValueError(
            f"{ALLOW_VAR} : titulaire illisible {morceau.strip()!r} pour « {nom} » — "
            f"un titulaire s'écrit « palier:id », palier parmi "
            f"{', '.join(PALIERS_TITULAIRES)} ; une exception de la plateforme n'a "
            f"pas de suffixe `@`. Format : {_FORMAT}.")
    return (palier, ident)


def _lire_exceptions(brut: str) -> list[Exception_]:
    """Toutes les entrées de `brut`, titulaires compris. Une entrée illisible LÈVE :
    une liste d'exceptions qu'on ne sait pas lire ne s'interprète pas « comme vide »,
    elle se signale."""
    lues: list[tuple[str, str, int, list]] = []
    suffixee = False   # la dernière entrée lue porte un `@` : un morceau sans `=` la suit
    for entree in brut.replace("\n", ",").split(","):
        entree = entree.strip()
        if not entree:
            continue
        if "=" not in entree:
            if not suffixee:
                raise ValueError(
                    f"{ALLOW_VAR} : entrée illisible {entree!r} — format attendu "
                    f"{_FORMAT}.")
            lues[-1][3].append(_titulaire(entree, lues[-1][0]))
            continue
        nom, _, destination = entree.partition("=")
        nom, destination = nom.strip(), destination.strip()
        destination, arobase, titulaire = destination.partition("@")
        destination = destination.strip()
        if not nom or not destination:
            raise ValueError(
                f"{ALLOW_VAR} : entrée illisible {entree!r} — format attendu "
                f"{_FORMAT}.")
        adresse, _, port = destination.rpartition(":")
        adresse = adresse.strip().strip("[]")
        try:
            ip = ipaddress.ip_address(adresse)
            numero = int(port)
        except ValueError:
            raise ValueError(
                f"{ALLOW_VAR} : destination illisible {destination!r} pour "
                f"« {nom} » — une exception se déclare par une ADRESSE littérale "
                "ET un port (« 127.0.0.1:9000 ») : jamais par un nom d'hôte, qui "
                "peut se déplacer sous nous, et jamais par une adresse seule, qui "
                "ouvrirait toute la machine.") from None
        lues.append((nom, str(ip), numero,
                     [_titulaire(titulaire, nom)] if arobase else []))
        suffixee = bool(arobase)
    return [Exception_(nom, adresse, port, frozenset(t)) for nom, adresse, port, t in lues]


def _valider(titulaire) -> Titulaire:
    """Un titulaire est un couple `(palier, id)` de chaînes : rien d'autre ne passe —
    surtout pas un booléen hérité de l'ancienne politique."""
    if (not isinstance(titulaire, tuple) or len(titulaire) != 2
            or not all(isinstance(x, str) for x in titulaire)):
        raise TypeError(f"titulaire d'egress invalide : {titulaire!r} — attendu un "
                        "couple (palier, id), `egress.titulaire_de(...)`, PLATEFORME, "
                        "AUCUN ou AGENT.")
    return titulaire


def declared_exceptions(brut: str | None = None, *,
                        titulaire: Titulaire) -> dict[tuple[str, int], str]:
    """Les exceptions déclarées qui valent pour `titulaire`, `(adresse, port) → nom` :
    pour la plateforme, celles sans suffixe ; pour un autre titulaire, celles qui le
    nomment EXACTEMENT ; pour personne (`AUCUN`) ni pour un agent (`AGENT`), aucune.

    Lit `OTO_EGRESS_ALLOW` quand `brut` est absent. Une entrée illisible LÈVE (et
    refuse le démarrage, `verifier_reglages`)."""
    if brut is None:
        brut = os.environ.get(ALLOW_VAR, "")
    exceptions: dict[tuple[str, int], str] = {}
    if _valider(titulaire) in (AUCUN, AGENT):
        return exceptions
    for e in _lire_exceptions(brut):
        if (titulaire in e.titulaires if titulaire != PLATEFORME else not e.titulaires):
            exceptions[(e.adresse, e.port)] = e.nom
    return exceptions


def verifier_reglages() -> None:
    """Au démarrage : `OTO_EGRESS_ALLOW` se lit en entier, ou le service ne démarre
    pas — jamais un repli sur une liste partielle ou vide. Les exceptions SANS
    titulaire (réservées à la plateforme) sont nommées dans le journal : une
    exception posée avant les titulaires pour une org ou un groupe ne les sert plus,
    et c'est ici qu'on le voit. Les noms seulement, jamais les adresses."""
    plateforme = [e.nom for e in _lire_exceptions(os.environ.get(ALLOW_VAR, ""))
                  if not e.titulaires]
    if plateforme:
        logger.warning(
            "exceptions d'egress réservées à la plateforme : %s ; une exception qui "
            "sert une org ou un groupe doit nommer son titulaire (@palier:id)",
            ", ".join(plateforme))


# ── l'instance elle-même ──────────────────────────────────────────────────────

def _vise_l_instance(hote: str) -> bool:
    """`hote` est l'hôte public de cette instance, l'un de ses sous-domaines, ou un hôte
    épinglé par org (`<slug>--<hôte public>`). Lève sans `OTO_MCP_PUBLIC_URL`."""
    from . import config
    nous = config.public_host().lower().rstrip(".")
    h = hote.lower().rstrip(".")
    return h == nous or h.endswith("." + nous) or h.endswith("--" + nous)


# ── le contrôle ───────────────────────────────────────────────────────────────

def resolved_addresses(hote: str, port: int) -> set[str]:
    """Toutes les adresses vers lesquelles cet hôte résout, sans scope IPv6.

    Fonction publique et séparée : c'est le point que les épreuves remplacent
    pour éprouver la garde sans dépendre du DNS de la machine qui les joue."""
    infos = socket.getaddrinfo(hote, port, proto=socket.IPPROTO_TCP)
    return {info[4][0].split("%", 1)[0] for info in infos}


def check_url(url: str, *, connector: str, field: str = "base_url",
              titulaire: Titulaire) -> None:
    """Refuse `url` si son hôte résout vers une adresse interne non déclarée.

    Ne rend rien quand la destination est publique ou déclarée. `connector` et
    `field` ne servent qu'au message : un opérateur doit savoir QUELLE carte
    corriger, pas seulement que « quelque chose » a été refusé.

    `titulaire` est OBLIGATOIRE : au nom de qui on sort. `AGENT` = la politique de
    l'URL CHOISIE PAR L'AGENT
    (`web_read`, une source `url` de fichier — oto#180) : la même décision sur ce
    qui est interne, mais AUCUNE exception ne s'applique. Les exceptions nommées
    de `OTO_EGRESS_ALLOW` couvrent la destination d'un credential posé par un
    administrateur (un pont hébergé en boucle locale) ; une URL tapée par un agent
    n'a pas à les hériter — sinon déclarer un pont pour une organisation
    l'ouvrirait à toute lecture de page de n'importe quelle autre. Le refus dit
    alors le geste de CE lecteur-là, pas celui d'un opérateur. Jusqu'au
    12/09/2026, ces deux lecteurs portaient chacun leur propre garde, écrite
    avant celle-ci : même intention, trois textes, et une plage refusée ici
    pouvait passer là.

    Pour un identifiant, `titulaire_de(palier, id)` de sa ligne du coffre : seules
    les exceptions qui le nomment s'appliquent (`declared_exceptions`)."""
    morceaux = urlsplit((url or "").strip())
    if morceaux.scheme not in ("http", "https"):
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` doit être une URL http(s) — "
            f"schéma `{morceaux.scheme or '∅'}` refusé.")
    hote = morceaux.hostname
    if not hote:
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` est une URL sans hôte.")
    port = morceaux.port or (443 if morceaux.scheme == "https" else 80)
    refuse_instance(hote, connector=connector, field=field)

    try:
        adresses = resolved_addresses(hote, port)
    except OSError as e:
        raise EgressRefused(
            f"connecteur `{connector}` : `{field}` — l'hôte `{hote}` ne résout "
            f"pas ({e}). La destination ne peut pas être contrôlée, donc elle "
            "n'est pas appelée.") from None

    decide(hote, port, adresses, connector=connector, field=field, titulaire=titulaire)


def refuse_instance(hote: str, *, connector: str, field: str) -> None:
    """Lève si `hote` est cette instance (#1499) — quelle que soit la politique."""
    if _vise_l_instance(hote):
        raise EgressRefused(
            f"`{connector}` : destination refusée. `{hote}` est oto lui-même (l'hôte "
            "de OTO_MCP_PUBLIC_URL) : un agent n'appelle pas oto par un connecteur, il "
            "passe par ses outils, où sa liste est tenue. Aucune exception ne s'y "
            f"applique. (Champ `{field}`.)")


def decide(hote: str, port: int, adresses, *, connector: str, field: str,
           titulaire: Titulaire) -> None:
    """LA décision, sur des adresses DÉJÀ résolues : lève si l'une d'elles est
    interne sans exception applicable — au `titulaire` de l'identifiant. Partagée par
    `check_url` (le refus précoce, qui explique) et par le transport épinglé
    (`egress_transport`, qui fait foi parce qu'il décide sur les adresses mêmes
    auxquelles il se connecte)."""
    _valider(titulaire)
    applicables = None   # lues au premier refus possible : le trafic public n'y touche pas
    for adresse in sorted(adresses):
        raison = internal_reason(adresse)
        if raison is None:
            continue
        if applicables is None:
            applicables = declared_exceptions(titulaire=titulaire)
        if applicables.get((adresse, port)) is not None:
            continue
        # Le déguisement : dire les DEUX. Un refus qui ne montre que l'adresse
        # résolue paraît absurde à qui a saisi un nom de domaine public, et un
        # refus qui ne montre que le nom saisi ne dit pas ce qui cloche.
        vu = (f"`{hote}`" if hote == adresse
              else f"`{hote}`, qui résout vers {adresse},")
        if titulaire == AGENT:
            raise EgressRefused(
                f"`{connector}` : destination refusée. {vu} est une adresse interne — "
                f"{raison}. Depuis oto on ne lit que l'internet public : le service "
                "tourne sur une machine dont le réseau interne porte des services "
                "d'administration, et une URL choisie dans une conversation ne "
                "l'atteint pas, sans exception. Si cette adresse est un service de "
                "ton organisation, il se déclare comme connecteur `api` par un "
                "administrateur (carte HTTP du tableau de bord) — et une adresse "
                "interne y est refusée aussi, sauf exception nommée par l'opérateur "
                f"de la plateforme. (Champ `{field}`.)")
        raise EgressRefused(
            f"connecteur `{connector}` : destination refusée. {vu} est une "
            f"adresse interne — {raison}. Un connecteur ne sort pas vers le "
            "réseau interne de la plateforme : cette adresse porte des services "
            "d'administration qu'un credential d'organisation ne doit pas "
            "pouvoir atteindre.\n"
            f"Si c'est une destination légitime (un pont hébergé), elle doit "
            f"être déclarée comme exception NOMMÉE dans la variable "
            f"{ALLOW_VAR} du service, pour le titulaire de cet identifiant, au "
            f"format « nom=adresse:port[@palier:id] » — ici "
            f"« mon-pont={adresse}:{port}{_suffixe(titulaire)} »{_note(titulaire)}. "
            f"Tant qu'elle n'y "
            f"figure pas l'appel est refusé : la liste ne se devine pas, elle se "
            f"déclare. (Champ `{field}` de la carte {connector}.)")


def _suffixe(titulaire: Titulaire) -> str:
    if titulaire == PLATEFORME:
        return ""
    if titulaire[0] in PALIERS_TITULAIRES:
        return f"@{titulaire[0]}:{titulaire[1]}"
    return "@<palier>:<id>"


def _note(titulaire: Titulaire) -> str:
    if titulaire == PLATEFORME or titulaire[0] in PALIERS_TITULAIRES:
        return ""
    if titulaire == AUCUN:
        return (" — le titulaire est l'équipe ou l'org dont la carte portera la clé, "
                "une fois posée")
    return (" — une clé personnelle de membre n'en reçoit aucune : la carte se pose "
            "au palier d'une équipe ou de l'org")
