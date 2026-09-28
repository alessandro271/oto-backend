---
title: Export par périmètre de propriétaire
type: reference
description: >-
  Extraire d'une base partagée tout ce qui appartient à un propriétaire, et rien
  d'autre, pour le verser dans une instance née par le démarrage normal
  (oto-backend#1088, ADR 0070 §7.6). Le classement déclaré de chaque table
  (possédée, indirecte, instance, exclue), le refus d'une table non classée, le
  périmètre dérivé d'orgs déclarées (et de leur tenant), les refus de l'extraction,
  et l'import dans une base née par le démarrage : tenant sur la ligne 1, comptes
  dénudés, secrets rechiffrés, vérification par relecture.
---

# Export par périmètre de propriétaire

`oto_mcp/export_perimetre/`. **État : classement, extraction, import et rechiffrement
existent, éprouvés de bout en bout sur des bases de test
(`tests/export_perimetre/test_import_bout_en_bout.py`). L'outil en ligne de commande et
la répétition à blanc sur une copie restent à faire (#1088).**

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

## Le périmètre : des orgs déclarées, et leur tenant

`perimetre.resoudre(conn, orgs)`. Les équipes, les comptes (membres d'org et d'équipe),
les **orgs personnelles** de ces comptes et le **tenant** qui les héberge s'en
dérivent. Le tenant d'une org est son tenant EFFECTIF, l'union des trois axes de
`db.tenants.org_tenant_slug`, lue par la même expression. Refus nommés :

- `ComptesPartages` : un compte aussi membre d'une org hors périmètre (décision du
  28/09/2026 : le refus reste) ;
- `TenantsMultiples` : des orgs de plusieurs tenants — la cible n'a qu'un tenant primaire ;
- `TenantPartage` : le tenant héberge aussi des orgs hors périmètre ;
- `ComptesHorsTenant` : un compte sans le préfixe `<slug>:` du tenant. Sur la cible, le
  tenant devient PRIMAIRE et ses subs y sont nus (`tenancy.qualify`) : un sub sans le
  préfixe est celui d'un autre annuaire.

Décisions d'Alexis du 28/09/2026 : le tenant part et devient la ligne 1 de la cible ;
tout le journal d'appels part ; les droits déclarés et nos acceptations légales
restent (« exclue ») ; les orgs personnelles suivent leurs comptes.

## L'extraction

`extraction.exporter(conn, orgs, sortie)` travaille dans **une** transaction
`REPEATABLE READ READ ONLY`, donc dans un instantané cohérent où la base elle-même refuse
toute écriture. La connexion reste en lecture seule après l'appel. Avant la première
ligne écrite, elle refuse dans trois cas :

- `SecretsChiffres` : une ligne exportée porte une valeur chiffrée avec **notre** clé
  maîtresse (`secrets` du classement : coffre, secret de signature d'un déclencheur, clé
  d'une transcription), et l'appelant n'a pas demandé `transporter_secrets=True`.
  Transportées, ces valeurs restent chiffrées sous notre clé dans le fichier ;
- `ReferencesHorsPerimetre` : une clé étrangère d'une ligne exportée pointe vers une
  ligne qui ne part pas (un lien de page vers la page d'autrui, une ligne exclue) ;
- une clé étrangère vers une table **instance** n'est pas un refus : le manifeste la
  relève (`references_instance`). La cible doit porter ces lignes. Les clés vers
  `tenants(id)` ne sont pas contrôlées : l'import les remappe toutes vers la ligne 1.

Les identifiants sont **préservés**. Le fichier contient une ligne JSON par ligne de
table (`{"t", "l"}`, le `row_to_json` de PostgreSQL), les parents avant leurs enfants,
puis le manifeste. Celui-ci porte le compte par table, les lignes omises des tables
exclues, le maximum de chaque séquence, l'inventaire hors base (clés d'Object Storage à
copier à part), l'instantané, la version de schéma, les colonnes de chaque table, le
tenant, la correspondance des comptes source → cible, les secrets transportés et
l'empreinte SHA-256 des lignes. Les horodatages sont écrits en UTC. Un export
existant ne s'écrase pas.

## L'import

`importation.importer(conn, fichier, cles=Cles(source, cible))` verse le fichier dans
une base **née par le démarrage** pour l'instance du propriétaire, en UNE transaction.
Avant d'écrire, elle refuse (`ImportRefuse`) : un fichier dont l'empreinte ou les
comptes ne sont pas ceux du manifeste, une version de schéma ou des colonnes
différentes, une base déjà peuplée (`orgs` ou `users`), un tenant primaire cible dont
le slug (`OTO_TENANT_PRIMAIRE_SLUG`) n'est pas celui de l'export, ou des secrets sans
les deux clés.

Trois remappages, et rien d'autre :

- **le tenant** : la ligne 1 semée par le démarrage prend les valeurs du tenant exporté,
  et toute clé vers `tenants(id)` vaut 1 ;
- **les comptes** perdent le préfixe `<slug>:` : toute VALEUR exactement égale à un sub
  du périmètre, ou à sa forme membre `<org>:<sub>`, est remplacée, à toute profondeur
  d'un JSON ;
- **les secrets** sont rechiffrés (`rechiffrement`) : déchiffrés sous la clé source et
  l'AAD de la ligne source, rechiffrés sous la clé cible et l'AAD de la ligne cible. Les
  AAD viennent des fonctions qui écrivent ces secrets. Le clair ne vit qu'en mémoire.
  ⚠️ L'AAD d'un credential de compte ou de membre contient le sub : c'est pourquoi le
  dénudage des comptes impose ce rechiffrement.

Les déclencheurs de la cible (journal des révisions, vecteur de recherche) sont
suspendus le temps de la transaction : l'import reproduit un état, il ne rejoue pas des
gestes. Les clés étrangères restent vérifiées. Les auto-références (une page sous une
page) se posent une fois la table remplie. Les séquences sont recalées sur le maximum
du manifeste.

**Vérification** : dans la même transaction, le périmètre est RELU sur la cible par la
lecture même de l'export (`extraction.ouvrir`). Par table, il faut le même nombre de
lignes et la même empreinte que les lignes écrites, sinon tout est annulé
(`VerificationEchouee`). Cette empreinte est une somme de hachés, indépendante de
l'ordre, de la forme canonique de chaque ligne : c'est la seule comparaison qui
survive aux remappages. L'empreinte brute du fichier, elle, est contrôlée avant toute
écriture.

⚠️ **Jamais contre la base servie** tant que l'outil n'a pas été répété à blanc sur une
copie : production et préproduction partagent la même base.
