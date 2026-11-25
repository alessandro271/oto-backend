"""Le TAMPON DE COMPATIBILITÉ des surfaces servies — une entrée par changement cassant.

Un changement cassant d'une surface servie (outil MCP, route REST, argument, forme
d'adresse, contrat de réponse) se fait en deux temps qui ne se mélangent pas :

1. **Le code métier est propre tout de suite.** Il ne connaît que la forme
   d'aujourd'hui : pas de repli, pas de double chemin, pas de « legacy ».
2. **L'ancienne forme est traduite EN BORDURE, ici.** Une entrée de ce module dit ce
   qui a changé, comment l'ancienne forme se traduit vers la nouvelle, et à quel bord
   (middleware MCP, adaptateur REST, authentification) la traduction s'applique.
   Rien en aval n'apprend qu'une ancienne forme existe.

Chaque entrée porte trois choses, sans exception :

- **un compteur d'usage** : chaque appel servi par l'ancienne forme laisse une trace
  dans le journal `tool_calls` (une clé de `args`, jamais la valeur reçue), et la
  requête qui le compte est écrite avec l'entrée ;
- **un critère de retrait** : l'entrée part quand le compteur est à ZÉRO chez tous les
  consommateurs qui la servent (règle du 25/09/2026, #966) — mesuré, jamais supposé ;
- **un réglage d'instance éteint par défaut** (pour une forme déjà RETIRÉE du code
  métier) : éteint, le refus du code métier est servi tel quel ; allumé, l'ancienne
  forme est traduite et tracée. Une valeur de réglage illisible refuse le démarrage
  (`verifier_reglages`) ;
- **une date de REVUE**, écrite ici : le jour où l'on va lire le compteur. Elle ne
  change RIEN au comportement servi — jamais une date couperet : un tampon allumé
  traduit tant qu'il est allumé.

Retrait d'une entrée : quand la trace ne montre plus aucun appel par l'ancienne forme
chez toutes les instances qui ont allumé son réglage. Revue sans zéro : on reporte la
date de revue, on ne coupe pas. Le document de décision est
`docs/compat-surfaces-servies.md` ; la table des entrées, `docs/alias-deprecies.md`.

**Les renommages de vocabulaire plus bas suivent la même règle (décision du
23/11/2026) : plus aucun couperet par date.** Un ancien nom (outil, chemin REST,
valeur d'un champ) reste servi tant que son usage compté n'est pas nul. Aucun texte
servi ne porte de date de retrait : chaque avis dit « déprécié, utilise X ; l'ancien
nom sera retiré avec un préavis » (`PREAVIS_DE_RETRAIT`), et les redirections des
anciens chemins REST portent `Deprecation: true`, sans `Sunset`. Leurs compteurs, dans
le journal `tool_calls` :

- `args.deprecated_name` = l'ancien nom d'outil sous lequel l'appel est arrivé
  (`calllog.apply_call_trace`, `session_org.nom_deprecie_de_l_appel`) ;
- `args.deprecated_value` = `"<champ>=<valeur reçue>"` quand `valeur_canonique` a
  réécrit une valeur dépréciée (faces MCP et REST) ;
- une ligne `kind='rest'` par redirection 308 d'un ancien chemin REST
  (`api.alias_routes`) : `tool` = verbe + chemin d'aujourd'hui, `args.deprecated_name`
  = l'ancien chemin GABARIT, jamais le chemin réel.

**Le retrait réel est une PR séparée**, ouverte quand ces mesures sont vides. C'est
elle qui pose une date ferme, au moins `PREAVIS_MOIS` (deux mois, art. 8.2 du contrat
de service) après le premier avis daté qu'elle servira. Les constantes `REVUE*` sont
des dates de REVUE, sans AUCUN effet sur ce qui est servi : une date de revue passée
ne change rien.

⚠️ **Ce que le module ne tient PAS — à ne pas croire tenu.** Le préavis est PASSIF :
`Deprecation` et l'avis en tête de description ou de réponse se voient
quand le consommateur inspecte ses réponses ; rien n'est poussé vers lui. Rendre le
préavis actif demande le canal de notification qui manque à toute la plateforme
(#766), pas un réglage de ce module.
"""
from __future__ import annotations

import calendar
import datetime
import os
from typing import Any, NamedTuple, Optional
from urllib.parse import quote

# ── Le préavis : sa durée, et d'où elle vient ───────────────────────────────
# **Cette durée est un ENGAGEMENT CONTRACTUEL, pas un réglage.** L'Art 8.2 du contrat
# de service promet un préavis de DEUX MOIS avant toute rupture d'interface. C'est la
# durée qu'on a écrite au client : la raccourcir est un manquement, pas une
# optimisation. Elle a valu 30 jours du 29/08 au 01/09/2026 — l'écart, et la référence
# de l'article, sont dans oto-backend#767. Un test en fait un plancher
# (`tests/test_alias_deprecies_outils.py`), pour que personne ne la « rationalise » un
# jour sans savoir ce qu'il touche : allonger reste libre, descendre sous deux mois
# rougit. ⚠️ Il ne produit AUCUNE date servie : c'est la PR de retrait qui datera le
# retrait, au moins cette durée après le premier avis daté qu'elle servira.
#
# ⚠️ La formulation exacte de l'article ne se recopie pas ici, et ce commentaire n'en
# tient pas lieu : pour arbitrer un cas limite — une rupture imposée par la sécurité,
# le point de départ du préavis — aller lire la pièce.
PREAVIS_MOIS = 2

#: Ce que tout avis de dépréciation SERVI dit du retrait — jamais un jour.
PREAVIS_DE_RETRAIT = "l'ancien nom sera retiré avec un préavis"

# Le jour où l'avis a commencé à être SERVI EN PRODUCTION — pas celui du merge :
# `main` est la préproduction, et un avis que le consommateur ne peut pas encore lire
# n'a averti personne. Les trois
# lots d'alias ont été mergés le 28/08/2026 et sont partis en production avec le tag
# `v1.159.0`, le 29/08/2026 — c'est ce jour-là que la fenêtre s'ouvre.
ANNONCE = datetime.date(2026, 8, 29)


def _plus_de_mois(depart: datetime.date, mois: int) -> datetime.date:
    """`depart` + `mois` mois CALENDAIRES — jamais une approximation en jours.

    Deux mois comptés en 60 jours tombent un jour trop tôt neuf fois sur douze :
    c'est exactement la famille d'écart que #767 et #768 corrigent, et elle penche
    toujours du même côté — contre celui à qui le préavis est dû.
    """
    rang = depart.month - 1 + mois
    annee, mois_cible = depart.year + rang // 12, rang % 12 + 1
    jour = min(depart.day, calendar.monthrange(annee, mois_cible)[1])
    return datetime.date(annee, mois_cible, jour)


# DATE DE REVUE du lot #519 : le jour où relire les compteurs d'usage (lot D, #526).
# Sans effet sur ce qui est servi — rien ne la formate, rien ne la compare au jour.
PREAVIS_DU = _plus_de_mois(ANNONCE, PREAVIS_MOIS)
REVUE = max(PREAVIS_DU, datetime.date(2026, 11, 30))


# ── Le connecteur Custom API renommé `api` (ex-`http`, 12/11/2026) ──────────
# Décision du propriétaire du produit : `http` était un nom de protocole, qui ne dit
# pas ce que le connecteur sert. Le connecteur s'appelle `api`, ses outils `api*` —
# mêmes ops, mêmes arguments, même effet. Comme tout renommage servi, il se DOUBLE :
# les anciens noms d'outils et les anciens chemins REST continuent de répondre, en
# disant le nom d'aujourd'hui.
#
# **Quand l'ancien nom part** : quand tous les consommateurs déclarés ont suivi
# (`.github/contrat-consommateurs.json`, règle du 25/09/2026) ET que l'usage compté
# des alias est nul — le journal `tool_calls` porte `args.deprecated_name` sur chaque
# appel servi par un ancien nom d'outil (`calllog.apply_call_trace`), et une ligne
# par redirection d'un ancien chemin REST. La date ci-dessous est une DATE DE REVUE,
# sans effet sur ce qui est servi.
ANNONCE_CONNECTEUR_API = datetime.date(2026, 11, 12)
REVUE_CONNECTEUR_API = max(_plus_de_mois(ANNONCE_CONNECTEUR_API, PREAVIS_MOIS),
                           datetime.date(2027, 1, 31))

#: Ancien nom d'outil → nom d'aujourd'hui, le même suffixe des deux côtés.
OUTILS_CONNECTEUR_API: dict = {f"http{suffixe}": f"api{suffixe}"
                               for suffixe in ("", "_act", "_get", "_post", "_doc")}

#: L'annonce du renommage est servie aux agents (`instructions`) jusqu'à ce jour
#: inclus — un mois. La description et chaque réponse de l'ancien nom le disent
#: ensuite, tant qu'il est servi.
ANNONCE_CONNECTEUR_API_JUSQU_AU = _plus_de_mois(ANNONCE_CONNECTEUR_API, 1)


def annonce_connecteur_api_du_jour(aujourd_hui: Optional[datetime.date] = None
                                   ) -> Optional[str]:
    """La ligne servie aux agents tant que le renommage est récent, None ensuite."""
    jour = aujourd_hui or datetime.date.today()
    if jour > ANNONCE_CONNECTEUR_API_JUSQU_AU:
        return None
    return ("The Custom API connector is now `api`, and its tools renamed — "
            + " · ".join(f"`{a}` → `{n}`" for a, n in OUTILS_CONNECTEUR_API.items())
            + ". Same arguments, same effect. The old names still answer as deprecated "
            "aliases and will be removed with prior notice: call the new ones.")


# ── Outils MCP (lot B1) ─────────────────────────────────────────────────────
# ancien nom SERVI → nom canonique. L'ancien reste listé et appelable tant que son
# usage n'est pas nul ; le bord du protocole (`middleware/alias.ToolAliasMiddleware`) rétablit
# le canonique AVANT que quoi que ce soit d'autre ne le lise, exactement comme pour
# le renommage par tenant — donc rien en aval n'apprend que l'alias existe : les
# gates, la denylist de visibilité, le journal `tool_calls` et les références
# `<tool:slug>` continuent de voir un seul nom pour un seul outil.
TOOLS: dict = {
    # (L'ancien nom du lot #519 est RETIRÉ, lot D, #526 : il est inconnu.)
    # Deux verbes de CONNEXION rangés sous le préfixe transverse (02/09/2026) : le
    # gate par connecteur résout au namespace du NOM, donc `oto_…` les servait à
    # tous les comptes — y compris ceux qui n'ont ni Salesforce ni Zoho. C'est ce
    # qu'Alexis a repéré en trouvant dans ses outils un connecteur qu'il n'utilise
    # pas. Renommés sous leur connecteur ; l'ancien nom reste appelable parce
    # qu'une procédure d'org le référence (mesuré, pas supposé).
    "oto_salesforce_connect": "salesforce_connect",
    "oto_zoho_connect": "zoho_connect",
    **OUTILS_CONNECTEUR_API,
}

#: La date de REVUE propre à un ancien nom d'outil, quand elle n'est pas `REVUE`.
_REVUE_DES_OUTILS: dict = {a: REVUE_CONNECTEUR_API for a in OUTILS_CONNECTEUR_API}


def date_de_revue(ancien: Optional[str] = None) -> datetime.date:
    """La date de REVUE de l'ancien nom d'outil `ancien` (ou du lot #519). Jamais
    servie : elle dit quand relire les compteurs, pas quand l'alias s'en va."""
    return _REVUE_DES_OUTILS.get(ancien, REVUE)


def avis(canonique: str) -> str:
    """L'avis qui PRÉFIXE la description d'un nom déprécié.

    En tête, pas en queue : beaucoup de clients tronquent une description longue,
    et un avis de dépréciation lu après 400 caractères n'a averti personne. C'est
    aussi la première chose que le modèle lit quand il choisit son outil.
    """
    return f"Déprécié : utilisez `{canonique}` ({PREAVIS_DE_RETRAIT}). "


def avis_d_appel(ancien: str) -> Optional[str]:
    """L'avis en tête de la RÉPONSE d'un appel fait sous un ancien nom — None si `ancien`
    n'est pas déprécié. Il nomme ce qui a été appelé et ce qui a servi : un agent qui
    ne relit pas sa liste d'outils l'apprend au geste."""
    canonique = TOOLS.get(ancien)
    if canonique is None:
        return None
    return (f"Déprécié : `{ancien}` est renommé `{canonique}` ({PREAVIS_DE_RETRAIT}). "
            f"L'appel a été servi par `{canonique}` ; "
            f"appelle désormais `{canonique}`, mêmes arguments.")


def tool_canonique(nom: str) -> str:
    """Le nom que le SERVEUR connaît. Un nom non déprécié passe inchangé."""
    return TOOLS.get(nom, nom)


def tools_deprecies_de(canonique: str) -> tuple:
    """Les anciens noms d'un outil, à servir à côté du sien. Vide si aucun."""
    return tuple(sorted(a for a, c in TOOLS.items() if c == canonique))


# ── Chemins REST (lot B2) ───────────────────────────────────────────────────
class AliasRest(NamedTuple):
    """Un ancien chemin REST, monté en **308** vers son chemin d'aujourd'hui.

`ancien` et `nouveau` s'écrivent chacun avec SES propres placeholders — ceux de
    la route réellement montée de chaque côté, pour que le chemin publié dans
    `/openapi.json` et dans la doc soit celui qu'on lit dans la table de routes.
    `params` porte l'écart quand un placeholder change de nom en route ; seule la
    VALEUR capturée voyage.
    """
    verbe: str
    ancien: str
    nouveau: str
    # placeholder de `ancien` → placeholder de `nouveau`, quand ils diffèrent.
    # Jamais muté (une valeur par défaut de NamedTuple est partagée).
    params: dict = {}
    # La date de REVUE propre à cet alias, quand elle diffère de `REVUE` — jamais
    # servie (plus d'en-tête `Sunset`, décision du 23/11/2026).
    revue: object = None


# ── Renommage `namespace` → `datastore` (08/09/2026) ────────────────────────
#
# ⚠️ **Ces alias sont DÉRIVÉS des routes réellement montées, pas recopiés.** Le
# renommage touche une vingtaine de couples (verbe, chemin) — les lister à la main,
# c'est en oublier un, et un chemin oublié ne lève rien : il rend 404 chez un
# consommateur qui croyait la redirection posée. La dérivation ne peut pas oublier.
#
# ⚠️ Et le vrai motif de leur existence n'est pas la douceur : **le front de la refonte
# ne PEUT PAS porter les deux noms.** Une garde de son dépôt exige que tout chemin
# appelé existe verbatim dans le contrat servi — déclarer un chemin que le back ne sert
# pas échoue, dans les deux sens. Sans ces alias, sa bascule et celle du back devraient
# coïncider à la seconde ; avec eux, chacune devient indépendante, et elles survivent à
# un retour arrière du back.
#
# Comme tout ce module : ils portent une date de REVUE, sans effet sur ce qui est servi.
ANNONCE_DATASTORE = datetime.date(2026, 9, 8)
REVUE_DATASTORE = _plus_de_mois(ANNONCE_DATASTORE, PREAVIS_MOIS)

#: L'ancien préfixe et le nouveau. Le placeholder change aussi de nom : seule la
#: VALEUR capturée voyage (cf. `cible`).
_DS_ANCIEN = "/api/datastore/namespaces"
_DS_NOUVEAU = "/api/datastores"
#: Les PARAMÈTRES renommés : ancien nom → (nouveau nom, date servie, ce qui ne change
#: pas). Une seule table pour les deux refus qui les nomment — la base des entrées de
#: capacité (`EntreeDatastore`) et l'enveloppe d'erreur des outils écrits à la main
#: (`error_taxonomy`, oto#135) — et pour le placeholder des alias de chemin.
#: ⚠️ La date est celle du REFUS servi (09/09), pas celle de l'annonce des chemins
#: (`ANNONCE_DATASTORE`, 08/09) : c'est la première que les agents lisent déjà.
PARAMETRES_RENOMMES = {
    "namespace": ("datastore", "2026-09-09",
                  "**the address does not change** (table name, number, "
                  "`slot:<name>` form), only the key switches"),
}
_DS_PARAMS = {ancien: v[0] for ancien, v in PARAMETRES_RENOMMES.items()}


def refus_parametre_renomme(ancien: str, valeur: Any = None,
                            outil: Optional[str] = None) -> Optional[str]:
    """Le refus d'un paramètre passé sous un nom RETIRÉ — `None` s'il n'a pas été renommé.

    Un seul texte, servi tel quel par la face capacité et par la face des outils écrits
    à la main. Jusqu'au 10/09/2026 la seconde disait « valeur(s) refusée(s) : namespace
    (Unexpected keyword argument) » — en anglais, sans le nom neuf : un agent y lisait
    un paramètre manquant et recomposait son appel à neuf (oto#135)."""
    if ancien not in PARAMETRES_RENOMMES:
        return None
    nouveau, date, inchange = PARAMETRES_RENOMMES[ancien]
    rejeu = (f"`{outil}` with `{nouveau}={valeur!r}`" if outil
             else f"the same call with `{nouveau}={valeur!r}`")
    return (f"`{ancien}` was renamed `{nouveau}` on {date} — the parameter no longer "
            f"exists under that name, and nothing was written. Replay {rejeu}: {inchange}. "
            f"⚠️ Do not hunt for another parameter: you provided the right value "
            f"under a retired name.")


def _alias_datastore() -> tuple:
    """Un alias par route datastore réellement montée — jamais une liste à la main.

    Lit les `RestBinding` des capacités du datastore et rend, pour chacune, l'ancien
    chemin équivalent. Un chemin ajouté demain hérite de son alias sans que personne
    n'y pense ; un chemin retiré perd le sien, au lieu de laisser une redirection qui
    mène nulle part.
    """
    from .capabilities.registry import CAPABILITIES  # tardif : évite un cycle

    out = []
    for cap in CAPABILITIES:
        rest = getattr(cap, "rest", None)
        chemin = getattr(rest, "path", None) if rest else None
        if not chemin or not chemin.startswith(_DS_NOUVEAU):
            continue
        ancien = _DS_ANCIEN + chemin[len(_DS_NOUVEAU):]
        ancien = ancien.replace("{datastore}", "{namespace}")
        out.append(AliasRest(getattr(rest, "verb", "GET"), ancien, chemin,
                             _DS_PARAMS, REVUE_DATASTORE))
    # Un chemin LITTÉRAL doit précéder un chemin à placeholder qui l'engloberait :
    # `…/namespaces` avant `…/namespaces/{namespace}`. Le tri par longueur puis par
    # nombre de placeholders donne cet ordre sans qu'on ait à le tenir à jour.
    return tuple(sorted(out, key=lambda a: (a.ancien.count("{"), len(a.ancien))))


#: Le préfixe REST du connecteur Custom API, avant et après son renommage.
_API_ANCIEN = "/api/orgs/{id}/connectors/http/"
_API_NOUVEAU = "/api/orgs/{id}/connectors/api/"
# Les routes NÉES après le renommage n'ont jamais eu d'ancien chemin : les applications de
# Custom API (`capabilities/connectors/api_apps.py`, 18/11/2026).
_API_NEES_APRES = (_API_NOUVEAU + "apps",)


def _alias_connecteur_api() -> tuple:
    """Un alias par route du connecteur `api` réellement montée — même dérivation que
    `_alias_datastore` : une route retirée perd son ancien chemin. Une route NÉE après le
    renommage (`_API_NEES_APRES`) n'en a jamais eu, et n'en reçoit pas. Le placeholder ne
    change pas de nom."""
    from .capabilities.registry import CAPABILITIES  # tardif : évite un cycle

    out = []
    for cap in CAPABILITIES:
        for rest in cap.rest_bindings():
            if rest.path.startswith(_API_NOUVEAU) and not rest.path.startswith(_API_NEES_APRES):
                ancien = _API_ANCIEN + rest.path[len(_API_NOUVEAU):]
                out.append(AliasRest(rest.verb, ancien, rest.path, {},
                                     REVUE_CONNECTEUR_API))
    return tuple(sorted(out, key=lambda a: (a.ancien.count("{"), len(a.ancien))))


def alias_rest_derives() -> tuple:
    """Tous les anciens chemins REST servis en 308, DÉRIVÉS des routes montées. (Les
    chemins du lot #519, déclarés à la main, sont RETIRÉS — lot D, #526.)"""
    return _alias_datastore() + _alias_connecteur_api()


def cible(alias: AliasRest, path_params: dict, query: str = "") -> str:
    """Le chemin de destination d'un alias, params de chemin injectés.

    La query string est REPORTÉE telle quelle : un client qui appelle
    `…/library?limit=200` et suit un 308 qui la perdrait recevrait 100 entrées au
    lieu de 200 — une régression qu'aucun code d'erreur ne signale.
    """
    chemin = alias.nouveau
    for nom, valeur in (path_params or {}).items():
        cible_nom = alias.params.get(nom, nom)
        chemin = chemin.replace("{" + cible_nom + "}", quote(str(valeur), safe=""))
    return f"{chemin}?{query}" if query else chemin


# ── Valeurs d'entrée renommées ──────────────────────────────────────────────
#
# Une VALEUR qu'un appelant passe dans un champ, renommée — pas une clé ni un chemin.
# **Forme de la coexistence** : l'entrée est RÉÉCRITE vers la valeur d'aujourd'hui
# avant toute autre lecture (autz comprise), la réponse porte `deprecation_warning`
# (sans date), et le journal compte l'usage (`args.deprecated_value`).
#
# Vide aujourd'hui : la famille « procédure » de `oto_resource` (`resource_type`,
# otomata-tech/oto#65) est RETIRÉE avec le lot D (#526) — l'ancienne valeur est
# inconnue, refusée comme toute valeur invalide.
#: champ → ({valeur d'hier → valeur d'aujourd'hui}, date de REVUE — jamais servie).
VALEURS: dict = {}


def valeur_canonique(champ: str, valeur: Any) -> tuple:
    """`(valeur d'aujourd'hui, avis)` — l'avis vaut `None` si la valeur n'est pas dépréciée.

    L'avis est le texte SERVI dans `deprecation_warning` : il nomme ce qu'on a reçu et
    ce qu'il faut envoyer — de quoi se corriger sans lire cette page. Sans date : le
    retrait n'en aura une qu'une fois l'usage nul.

    Une réécriture est COMPTÉE : le journal de l'appel courant (face MCP comme REST)
    porte `args.deprecated_value = "<champ>=<valeur reçue>"`
    (`session_org.noter_valeur_depreciee`)."""
    renommees, _revue = VALEURS.get(champ, ({}, None))
    nouvelle = renommees.get(valeur) if isinstance(valeur, str) else None
    if nouvelle is None:
        return valeur, None
    from . import session_org  # tardif : ce module est lu très tôt
    session_org.noter_valeur_depreciee(f"{champ}={valeur}")
    return nouvelle, (
        f"Déprécié : `{champ}={valeur}` s'écrit `{champ}={nouvelle}` "
        f"(l'ancienne valeur sera retirée avec un préavis). L'appel a été servi "
        f"comme `{nouvelle}` ; envoie désormais `{nouvelle}`.")


# ── Tampon : un tableau désigné par son NOM (retiré du code le 08/11/2026) ─────
#
# Le code métier ne connaît que le NUMÉRO (`ns_id`) et `slot:<nom>` : un nom y est
# refusé (`datastore.errors.TableauParNom`, `400 datastore_by_name`, #1786), et ce
# refus reste la règle. Une instance dont des consommateurs désignent encore un
# tableau par son nom ALLUME ce tampon (`OTO_MCP_COMPAT_TABLEAU_PAR_NOM`, éteint par
# défaut) : le nom est alors traduit en numéro AU BORD, avec la résolution d'avant le
# retrait (propriétaire, org active, partages — `db.resolve_datastore_ns`), puis le
# code métier reçoit le numéro et applique toutes ses gardes.
#
# Les bords qui appellent `traduire_arguments` / `traduire_tableau` — quatre, une
# seule traduction :
#   - MCP, appel direct d'un outil : `middleware/compat.CompatMiddleware` ;
#   - MCP, appel relayé (`oto_call`, recettes) : `tools/meta.executer_cible` ;
#   - REST, toute capacité : `capabilities/_rest_adapter` (chemin `{datastore}` et
#     champs déclarés du corps) ;
#   - REST, jeton porté : `api/base._authenticate` (le tableau du chemin, jugé sur la
#     portée par son numéro).
#
# **Compteur** : chaque désignation par nom laisse `args.ref_kind = 'name'` dans le
# journal `tool_calls` (#1802), qu'elle soit traduite (`ok`) ou refusée
# (`refused(datastore_by_name)` / `error`) ; `index_releve.MESURE_TABLEAU_PAR_NOM` les
# compte par face, org, compte et jeton.
# **Retrait** : quand ce compteur est à zéro chez toutes les instances qui ont allumé
# le tampon. `REVUE_COMPAT_TABLEAU_PAR_NOM` est le jour où l'on va le lire — une date
# de REVUE, sans aucun effet sur ce qui est servi (allumé, le tampon traduit, avant
# comme après). Échéance suivie : #1296.
ENV_COMPAT_TABLEAU_PAR_NOM = "OTO_MCP_COMPAT_TABLEAU_PAR_NOM"
REVUE_COMPAT_TABLEAU_PAR_NOM = datetime.date(2026, 12, 14)

#: Les surfaces où `datastore` NOMME un tableau à créer : jamais traduit.
_NOMME_UN_TABLEAU_NEUF = frozenset({"data_create_datastore",
                                    "me.datastore.create_datastore"})


def compat_tableau_par_nom_allume() -> bool:
    """Le réglage d'instance, tel qu'il est déclaré — lève sur une valeur illisible."""
    from . import interrupteurs
    return interrupteurs.oui_non(ENV_COMPAT_TABLEAU_PAR_NOM,
                                 os.environ.get("OTO_MCP_COMPAT_TABLEAU_PAR_NOM"))


def verifier_reglages() -> None:
    """Au démarrage : un réglage de tampon illisible refuse de démarrer."""
    compat_tableau_par_nom_allume()


def _adresses_de_tableau(surface: Optional[str], args: dict) -> list:
    """`(conteneur, clé)` de chaque adresse de tableau que `args` porte pour `surface`
    (nom d'outil MCP, ou clé de capacité pour une route REST sans outil) — la seule
    déclaration de l'endroit où un tableau se désigne dans des arguments."""
    from .datastore.errors import est_un_nom_de_tableau
    if not isinstance(args, dict):
        return []
    lieux: list = []
    if surface not in _NOMME_UN_TABLEAU_NEUF and "datastore" in args:
        lieux.append((args, "datastore"))
    if (surface == "oto_project" and args.get("target_type") == "tableau"
            and args.get("op") in ("link", "unlink")):
        lieux.append((args, "target_ref"))
    if surface == "oto_fleet" and args.get("op") == "create":
        lieux.append((args, "namespace"))
    if surface == "oto_apps" and isinstance(args.get("manifest"), dict):
        tables = args["manifest"].get("tables")
        for t in (tables.values() if isinstance(tables, dict) else ()):
            if isinstance(t, dict):
                lieux.append((t, "table"))
    return [(c, k) for c, k in lieux
            if isinstance(c.get(k), str) and est_un_nom_de_tableau(c[k])]


def _portees_de_jeton(surface: Optional[str], args: dict) -> list:
    """Les dictionnaires `{tableau: droit}` d'une portée de jeton émise par
    `me.token.create` (la portée, et le plafond d'un émetteur) qui nomment un tableau
    par son nom."""
    from .datastore.errors import est_un_nom_de_tableau
    if surface != "me.token.create" or not isinstance(args, dict):
        return []
    scopes = args.get("scopes")
    portees = [scopes, (scopes or {}).get("issue") if isinstance(scopes, dict) else None]
    return [p["namespaces"] for p in portees
            if isinstance(p, dict) and isinstance(p.get("namespaces"), dict)
            and any(est_un_nom_de_tableau(k) for k in p["namespaces"])]


def a_traduire(surface: Optional[str], args: dict) -> bool:
    """`args` porte-t-il un nom de tableau que le tampon traduirait ? Sans base : sert
    au bord asynchrone à ne quitter la boucle que s'il y a quelque chose à faire."""
    return bool(_adresses_de_tableau(surface, args) or _portees_de_jeton(surface, args))


def traduire_tableau(adresse: str, sub: Optional[str]) -> Optional[str]:
    """Le NUMÉRO (en texte) du tableau que le nom `adresse` désigne pour `sub`, dans
    son org active — la résolution d'avant le retrait du nom. `None` si le nom ne
    désigne aucun tableau visible : l'appelant le refuse comme INTROUVABLE.

    Compte la désignation (`ref_kind = name`) et, sur la face MCP, dit à l'appelant le
    numéro à passer. Ne juge rien d'autre : le code métier reçoit le numéro et applique
    ses gardes (écriture, vue bornée, projet partagé)."""
    from . import db, session_org
    from .datastore.core import make_store
    session_org.noter_forme_tableau(session_org.REF_NAME)
    if not sub:
        return None
    org_ids, group_ids = make_store(sub)._active_scope()
    ns = db.resolve_datastore_ns(adresse, sub=sub, org_ids=org_ids, group_ids=group_ids)
    if not ns:
        return None
    ns_id = str(int(ns["id"]))
    session_org.noter_avis(
        f"⚠️ Table `{adresse}` was addressed by its NAME — accepted, deprecated: pass "
        f"`datastore={ns_id}`. The NUMBER is the documented form; a table name is "
        "refused wherever this compatibility is off (`datastore_by_name`).")
    return ns_id


class TableauIntrouvable(Exception):
    """Un nom de tableau que le tampon n'a pas pu traduire : refusé comme introuvable."""

    def __init__(self, adresse: str):
        super().__init__(f"datastore `{adresse}` unknown")
        self.adresse = adresse


def traduire_arguments(surface: Optional[str], args: dict, sub: Optional[str]) -> None:
    """Traduit EN PLACE chaque nom de tableau que `args` porte pour `surface`, si le
    tampon est actif ; ne fait rien sinon (le code métier refuse le nom). Lit la base :
    hors de la boucle. Lève `TableauIntrouvable` sur un nom qui ne désigne rien."""
    from .datastore.errors import est_un_nom_de_tableau
    if not a_traduire(surface, args) or not compat_tableau_par_nom_allume():
        return
    for conteneur, cle in _adresses_de_tableau(surface, args):
        ns_id = traduire_tableau(conteneur[cle].strip(), sub)
        if ns_id is None:
            raise TableauIntrouvable(conteneur[cle])
        conteneur[cle] = ns_id
    for portee in _portees_de_jeton(surface, args):
        traduite = {}
        for nom, droit in portee.items():
            ns_id = traduire_tableau(nom.strip(), sub) if est_un_nom_de_tableau(nom) else nom
            if ns_id is None:
                raise TableauIntrouvable(nom)
            traduite[ns_id] = droit
        portee.clear()
        portee.update(traduite)


# (Clés de capacité et clés de réponse du lot #519 — `CAPACITES`, `CLES` et leur
# doublage : RETIRÉES, lot D, #526. Une réponse ne sert plus que les noms d'aujourd'hui.)


# ── Codes d'erreur (lot B3) ─────────────────────────────────────────────────
# ancien code → code d'aujourd'hui. Un code d'erreur ne se DOUBLE pas — il n'y a
# qu'un champ `error` — donc le nouveau prend la place, et l'ancien est conservé dans
# `details.legacy_code`. Un client qui teste `error == "unknown_doctrine"` a le temps
# du préavis pour aller lire `details.legacy_code`, ou mieux, le nouveau code.
CODES: dict = {
    "unknown_doctrine": "unknown_guide",
}


def details_avec_code_dhier(code_actuel: str, details=None) -> dict:
    """Les `details` d'un refus, augmentés du code d'hier quand il y en a un."""
    ancien = next((a for a, n in CODES.items() if n == code_actuel), None)
    if not ancien:
        return details or {}
    return {**(details or {}), "legacy_code": ancien}


# ── Noms de schéma OpenAPI (lot B3) ─────────────────────────────────────────
# ancien nom de composant → nom d'aujourd'hui. Publié dans `components.schemas` comme
# un `$ref` vers le nouveau, marqué déprécié : un client généré qui référence
# `#/components/schemas/DoctrineMeta` continue de résoudre.
#
# ⚠️ Cette table ne porte que les noms qui étaient VRAIMENT des composants. Un modèle
# `Output` de premier niveau n'en est pas un — son schéma est INLINE dans la réponse
# 200, et son nom n'y apparaît que comme `title`, ce qu'aucun `$ref` ne peut viser.
# `DoctrineView` était dans ce cas ; le renommer n'engage personne.
SCHEMAS: dict = {
    "DoctrineMeta": "GuideMeta",
}
