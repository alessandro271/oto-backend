---
title: Export par périmètre de propriétaire
type: reference
description: >-
  Extraire d'une base partagée tout ce qui appartient à un propriétaire, et rien
  d'autre, pour le verser dans une instance née par le démarrage normal
  (oto-backend#1088, ADR 0070 §7.6). Le classement déclaré de chaque table
  (possédée, indirecte, instance, exclue), le refus d'une table non classée, le
  périmètre dérivé d'orgs déclarées, et les refus de l'extraction : compte partagé,
  secret chiffré, référence hors périmètre.
---

# Export par périmètre de propriétaire

`oto_mcp/export_perimetre/`. **État : le classement et l'extraction en lecture seule
existent. Le rechiffrement des secrets, l'import et l'outil en ligne de commande
restent à faire (#1088).**

## Le classement : chaque table, une classe

`classement.CLASSEMENT` donne une entrée à **chaque** table du schéma réel :

| classe | ce qui part | la règle |
|---|---|---|
| **possédée** | les lignes du périmètre | directe : `ParOrg`, `ParSub`, `ParGroupe`, `ParEntite` (couple polymorphe `owner_type`/`owner_id`) |
| **indirecte** | les lignes dont le parent part | `Via` seulement, par une FK réelle (`fk=True`) ou logique (`fk=False`, id polymorphe en texte) |
| **instance** | rien : la cible naît avec les siennes | aucune ; une raison écrite |
| **exclue** | rien, mais le manifeste **compte** ce qui n'est pas parti | une règle et une raison (notre commerce, nos documents légaux, nos relances) |

⚠️ **Ajouter une table au schéma, c'est la classer dans le même commit.**
`decouverte.verifier_classement` refuse, en listant **toutes** les anomalies d'un coup :
une table non classée, une entrée sans table, une colonne nommée qui n'existe pas, un
`Via fk=True` sans la clé étrangère correspondante, un héritage d'une table qui ne part
pas. Le garde-fou est `tests/export_perimetre/test_classement_couvre_schema.py`, sur le
schéma que monte `init_db` et pas sur une reconstitution. On prouve qu'il mord en lui
présentant chaque anomalie.

⚠️ Une ligne dont `org_id` est NULL (droit « personne, partout » de #1089, appel hors
de toute org) appartient à son **compte** : les tables à `org_id` nullable combinent
`ParOrg` et `ParSubSansOrg`, sans quoi ces lignes tomberaient hors du périmètre en
silence.

`ParEntite` ne retient jamais `platform` ni `tenant` : ces lignes sont celles de
l'instance. Un membre s'y lit en `'<org_id>:<sub>'`.

## Le périmètre : des orgs déclarées

`perimetre.resoudre(conn, orgs)`. Les équipes, les comptes (membres d'org et d'équipe)
et les **orgs personnelles** de ces comptes s'en dérivent. ⚠️ Un compte qui est aussi
membre d'une org hors périmètre fait **refuser** l'export (`ComptesPartages`, qui le
nomme) : rien ne permet d'attribuer ses données personnelles, c'est une décision humaine.

## L'extraction

`extraction.exporter(conn, orgs, sortie)` travaille dans **une** transaction
`REPEATABLE READ READ ONLY`, donc dans un instantané cohérent où la base elle-même refuse
toute écriture. La connexion reste en lecture seule après l'appel. Avant la première
ligne écrite, elle refuse dans trois cas :

- `SecretsChiffres` : une ligne exportée porte une valeur chiffrée avec **notre** clé
  maîtresse (`secrets` du classement : coffre, secret de signature d'un déclencheur, clé
  d'une transcription). La cible ne peut pas la lire. L'AAD contient l'identité du
  propriétaire, donc un rechiffrement devra préserver les identifiants ;
- `ReferencesHorsPerimetre` : une clé étrangère d'une ligne exportée pointe vers une
  ligne qui ne part pas (un lien de page vers la page d'autrui, une ligne exclue) ;
- une clé étrangère vers une table **instance** n'est pas un refus : le manifeste la
  relève (`references_instance`, par exemple le tenant des orgs). La cible doit porter
  ces lignes.

Les identifiants sont **préservés**. Le fichier contient une ligne JSON par ligne de
table (`{"t", "l"}`, le `row_to_json` de PostgreSQL), les parents avant leurs enfants,
puis le manifeste. Celui-ci porte le compte par table, les lignes omises des tables
exclues, le maximum de chaque séquence, l'inventaire hors base (clés d'Object Storage à
copier à part), l'instantané, la version de schéma et l'empreinte SHA-256 des lignes.
Un export existant ne s'écrase pas.

⚠️ **Jamais contre la base servie** tant que l'outil n'a pas été répété à blanc sur une
copie : production et préproduction partagent la même base.
