---
title: Changement cassant d'une surface servie — tampon en bordure, retrait au compteur zéro
type: explanation
description: >-
  La règle qui encadre tout changement cassant d'une surface servie (outil MCP,
  route REST, argument, forme d'adresse, contrat de réponse) : le code métier
  passe tout de suite à la forme d'aujourd'hui, l'ancienne forme est traduite en
  bordure par une entrée du tampon de compatibilité (oto_mcp/deprecations.py),
  allumée par instance, tracée, et retirée quand son compteur est à zéro chez
  toutes les instances qui l'ont allumée. À charger avant de casser une surface
  servie, avant d'ajouter ou de retirer une entrée du tampon.
---

# Changement cassant d'une surface servie — tampon en bordure, retrait au compteur zéro

Décision d'Alexis du 23/11/2026. Ce dépôt n'a plus d'ADR et n'a pas de
`docs/conception/` : la décision vit ici, à côté de `alias-deprecies.md` (la table des
entrées).

## La règle

Un changement cassant d'une surface servie se fait en deux temps qui ne se mélangent
pas :

1. **Le code métier est propre tout de suite.** Il ne connaît que la forme
   d'aujourd'hui. Pas de repli, pas de double chemin, pas de « legacy » dans le
   métier : un nom de tableau y est refusé, un ancien nom d'outil y est inconnu.
2. **L'ancienne forme est traduite en bordure**, par une entrée du **tampon de
   compatibilité** — `oto_mcp/deprecations.py`, le seul module qui la connaisse. La
   traduction s'applique au bord (middleware MCP, adaptateur REST, authentification),
   avant que le métier ne lise quoi que ce soit.

Chaque entrée du tampon porte quatre choses, sans exception :

| | |
| --- | --- |
| **Un réglage d'instance, éteint par défaut** | `OTO_MCP_COMPAT_<ENTRÉE>`, déclaré dans `env_inventory.py`. Éteint, le refus du métier est servi tel quel. Allumé, l'ancienne forme est traduite. Une valeur illisible refuse le démarrage (`deprecations.verifier_reglages`). |
| **Une trace par appel** | Une clé dans les `args` de la ligne du journal `tool_calls`, jamais la valeur reçue. Elle se pose sur la face MCP (relevé de l'appel) comme sur la face REST (ligne de route), sur un appel traduit comme sur un appel refusé. |
| **Un critère de retrait** | Le compteur à **zéro** chez **toutes** les instances qui ont allumé le réglage (règle du 25/09/2026, #966). Mesuré, jamais supposé. |
| **Une date de REVUE** | Une constante du module, et une issue `[AAAA-MM-JJ]` (label `échéance`). C'est le jour où l'on va lire le compteur. Elle ne change **rien** au comportement servi : ce n'est jamais une date couperet. |

Les textes servis (descriptions, guides, OpenAPI) ne documentent que la forme
d'aujourd'hui. Une réponse servie par l'ancienne forme peut le dire (« accepted,
deprecated: pass … »), jamais la recommander.

## Pourquoi

- **Le code reste propre.** Le métier ne porte ni l'ancienne forme ni sa résolution.
  Retirer l'entrée revient à supprimer quelques lignes du tampon, sans toucher au
  métier.
- **Les consommateurs ne cassent pas.** Ils vivent hors de ce dépôt : instances
  tierces, intégrations à jeton, apps, déclencheurs, procédures écrites avant le
  changement. Un retrait sec ne casse rien en CI. Il casse en production, chez
  quelqu'un d'autre. Le retrait du nom de tableau (#1786) bloquait ainsi la montée de
  version d'une instance dont des appels désignaient encore des tableaux par leur nom.
- **L'usage est mesuré.** La trace dit qui se sert encore de l'ancienne forme, par
  instance, org, compte, jeton et outil. Le retrait se décide sur un compteur, pas sur
  une date ni sur une impression.
- **Par instance.** Une instance qui n'a plus de consommateur de l'ancienne forme
  garde le refus (réglage éteint). Seule celle qui en a besoin l'allume, et elle sait
  qu'elle l'a fait.

## Ajouter une entrée

1. Le métier passe à la forme d'aujourd'hui, avec son refus nommé.
2. Dans `deprecations.py`, une section :
   - le réglage (`ENV_COMPAT_…`, lu par `interrupteurs.oui_non`, avec le nom littéral
     dans `os.environ.get("…")` pour que l'inventaire le voie) et sa vérification dans
     `verifier_reglages` ;
   - la date de revue (`REVUE_COMPAT_…`) ;
   - la traduction, une seule fonction, appelée par les bords existants
     (`middleware/compat.CompatMiddleware`, `tools/meta.executer_cible`,
     `capabilities/_rest_adapter`, et `api/base._authenticate` si la forme touche au
     chemin) ;
   - la trace : une clé de `args` (ajoutée à `server._TRACED_ARGS` si elle passe par
     le relevé MCP), et la requête qui la compte.
3. La variable dans `env_inventory.py` (classe `REGLAGE`, défaut `"0"`), puis
   `scripts/generer_env_example.py`.
4. Une ligne dans `docs/alias-deprecies.md`, et une issue `[date de revue] …` sous
   l'issue de fond.
5. Les tests : éteint, rien ne change ; allumé, traduction et trace sur chaque face ;
   l'ancienne forme qui ne désigne rien reste refusée comme avant ; la date de revue
   passée ne change rien ; le métier refuse toujours l'ancienne forme.

## Décider du retrait

À la date de revue, l'exploitation de chaque instance qui a allumé le réglage joue la
requête de l'entrée sur sa base (lecture seule, `statement_timeout`).

- **Zéro partout** : on retire l'entrée (section du tampon, bords qui n'ont plus rien
  à traduire, variable d'inventaire, ligne de la table) et on éteint le réglage dans
  la déclaration des instances.
- **Pas zéro** : on nomme les appelants (org, compte, jeton, outil), on les fait
  migrer, et on reporte la date de revue. On ne coupe pas.

## Les entrées

### Tableau désigné par son NOM (`OTO_MCP_COMPAT_TABLEAU_PAR_NOM`)

- **Changement** : #1786. Un tableau se désigne par son numéro (`ns_id`) ou
  `slot:<nom>`, et un nom est refusé (`400 datastore_by_name`).
- **Traduction** : `deprecations.traduire_arguments` / `traduire_tableau`. La
  résolution est celle d'avant #1786 (`db.resolve_datastore_ns` dans la portée active
  : perso, orgs, équipes, partages). Le métier reçoit ensuite le numéro et applique
  toutes ses gardes (écriture, vue bornée, projet partagé). Un nom qui ne désigne
  rien est refusé comme **introuvable** (`datastore_not_found`). Les lieux couverts
  sont ceux que #1786 a fermés : l'argument `datastore` de tout outil et de toute
  route `/api/datastores/{datastore}/…`, sauf la création d'un tableau ;
  `oto_project op=link|unlink` (`target_ref` d'un tableau) ; `oto_fleet op=create`
  (`namespace`) ; `oto_upload_url` / `oto_import` ; le manifeste d'app
  (`tables.*.table`) ; la portée d'un jeton émis par `me.token.create` (et son
  plafond d'émetteur) ; le tableau du chemin jugé sur la portée d'un jeton porté.
- **Trace** : `args.ref_kind = 'name'` (#1802). Traduit : `ok = true`. Refusé :
  `error` ou `result_shape = refused(datastore_by_name)`.
- **Requête** : `index_releve.MESURE_TABLEAU_PAR_NOM`, servie par l'index partiel
  `idx_tool_calls_ref_nom`, compte par face, org, compte et jeton. Ajouter
  `tool` et `ok` pour l'outil et l'issue :

  ```sql
  SET statement_timeout = '15s';
  SELECT kind AS face, tool, org_id, sub, token_kind, token_id, ok,
         count(*) AS appels, min(created_at) AS premier, max(created_at) AS dernier
    FROM tool_calls
   WHERE args->>'ref_kind' = 'name'
     AND created_at >= now() - interval '30 days'
   GROUP BY 1, 2, 3, 4, 5, 6, 7
   ORDER BY appels DESC;
  ```
- **Date de revue** : 14/12/2026 (`REVUE_COMPAT_TABLEAU_PAR_NOM`, issue #1296).
- **Non couvert** : le jeton émis par un admin plateforme pour un autre compte
  (`platform.token.create`), dont la portée se résout chez la cible. Il reste refusé
  par nom.
