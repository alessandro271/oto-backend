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
  dénudés, secrets rechiffrés à l'export, vérification par relecture, commande
  `oto-mcp perimetre`.
---

# Export par périmètre de propriétaire

`oto_mcp/export_perimetre/`. **État : classement, extraction avec rechiffrement, import
par lots et commande `oto-mcp perimetre` existent, éprouvés de bout en bout sur des
bases de test (`tests/export_perimetre/test_import_bout_en_bout.py`). La répétition à
blanc sur une copie reste à faire (#1088).**

## La commande

```bash
# Chez NOUS, sur la base source (DATABASE_URL, OTO_MCP_MASTER_KEY, OTO_MCP_S3_* de notre
# instance) — écrit perimetre.jsonl et perimetre.jsonl.objets.tar :
OTO_EXPORT_CLE_CIBLE=<clé de l'instance cible> \
  oto-mcp perimetre export --org 12 [--org 13 …] --sortie perimetre.jsonl
# Sur l'instance cible, née par le démarrage (sa DATABASE_URL, SA clé maîtresse, SON
# stockage OTO_MCP_S3_*) — les deux fichiers côte à côte :
oto-mcp perimetre import perimetre.jsonl
```

Un refus s'imprime nommé et sort en code 2, sans rien écrire. Le résumé ne cite jamais
une clé, seulement l'empreinte de la clé cible.

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

- `SecretsChiffres` : une ligne exportée porte une valeur chiffrée (`secrets` du
  classement : coffre, secret de signature d'un déclencheur, clé d'une transcription)
  et l'appelant n'a pas donné la clé de l'instance cible (`cle_cible`) ;
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
tenant (id, slug, nom), la correspondance des comptes source → cible, le compte des
secrets, l'EMPREINTE de la clé cible (`rechiffrement.empreinte_cle`, jamais la clé) et
l'empreinte SHA-256 des lignes. Les horodatages sont écrits en UTC. Un export existant
ne s'écrase pas.

## Les secrets : rechiffrés CHEZ NOUS, À L'EXPORT

Décision d'Alexis du 28/09/2026 : **notre clé maîtresse ne sort jamais de notre
infrastructure**. L'export tourne chez nous, sous notre clé (`OTO_MCP_MASTER_KEY`), et
reçoit la clé de l'instance cible pour cette seule exécution. Il déchiffre chaque
secret sous notre clé et l'AAD de la ligne source, puis le rechiffre sous la clé cible
et l'AAD de la ligne CIBLE. Le fichier ne porte QUE des secrets chiffrés pour la cible.
Il n'y a pas d'autre chemin : aucun mode ne transporte un secret sous notre clé.

⚠️ L'AAD d'un credential de compte ou de membre contient le sub, et le sub change à
l'import. L'export calcule donc la ligne cible par la fonction même de l'import,
`transformation.Transformation`, et il n'en existe qu'une. Les AAD viennent des
fonctions qui écrivent ces secrets (`credentials_store._aad`,
`runner_hook._aad_du_secret`, `transcription_worker._aad`). Le clair ne vit qu'en mémoire.

## Les objets du stockage objet : une archive scellée pour la cible

Décision d'Alexis du 28/09/2026 : les objets voyagent par une **archive**, et par elle
seule. Il n'y a ni copie directe d'un seau à l'autre, ni URL signée.

- **Quels objets** (`objets`) : ceux qu'une ligne du périmètre désigne, par une CLÉ
  (`project_files.s3_key`, `transcription_jobs.audio_key`) ou par une URL de notre
  stockage public, où qu'elle soit. Cela couvre les colonnes (`users.avatar_url`,
  `orgs.logo_url`, `project_files.public_url`) comme les contenus : une image déposée
  par un agent (`images/<sub>/…`) n'a d'autre trace que son URL collée dans une page,
  un tableau ou un JSON. L'export cherche `<base publique>/<clé>` dans chaque ligne
  écrite (`cles_dans`). Un banc rougit si une colonne `hors_base` n'est classée ni clé
  ni URL.
- **L'archive** (`<sortie>.objets.tar`) : un membre par objet, nommé par sa clé et
  scellé (`crypto.seal`, AES-256-GCM) sous la clé de l'instance CIBLE, avec une AAD
  qui le lie à sa clé d'objet. Elle est écrite chez nous, depuis notre stockage
  (`stockage`, `media_store`). Le manifeste l'inscrit (`objets` : nom, empreinte
  SHA-256, notre base publique, et par objet la taille et l'empreinte du clair). Un
  objet absent de notre stockage refuse, tous nommés, et un export refusé ne laisse
  derrière lui ni lignes ni archive.
- **À l'import**, l'archive est vérifiée avant toute écriture. Elle se verse dans le
  stockage de la cible, avec ses propres identifiants, après la relecture et avant la
  validation. Chaque objet est déchiffré sous la clé de l'instance, comparé au
  manifeste, écrit sous la MÊME clé, puis relu. Un objet déjà là avec la même
  empreinte est sauté : un import interrompu se reprend.
- **Les URL** sont réécrites par la `Transformation` : `<notre base>/` devient `<base
  cible>/` dans toute valeur texte, colonne ou contenu. La base cible est celle que la
  cible déclare (`media_store.public_base`), sans défaut de notre côté. La relecture
  refuse s'il subsiste une URL de notre stockage dans le périmètre.

Les archives froides du journal mêlent tous les propriétaires : elles restent hors
périmètre.

## Les partages hors périmètre : omis, comptés

`resource_grants` et `grants` désignent leur destinataire par un couple polymorphe,
sans clé étrangère (`classement` : `destinataire`). Décision du 28/09/2026 : une ligne
dont le destinataire n'est pas du périmètre **ne part pas**. Sur la cible, ce
destinataire n'existe pas, et la ligne y emporterait l'identité d'un tiers. Le
manifeste compte ces lignes (`partages_omis`).

## L'import

`importation.importer(conn, fichier)` verse le fichier dans une base **née par le
démarrage** pour l'instance du propriétaire, en UNE transaction. L'import ne connaît
que la clé de SON instance. Avant d'écrire, il refuse (`ImportRefuse`) dans ces cas :

- un fichier dont l'empreinte ou les comptes ne sont pas ceux du manifeste ;
- une version de schéma ou des colonnes différentes ;
- une base déjà peuplée (`orgs` ou `users`) ;
- un tenant primaire cible dont le slug (`OTO_TENANT_PRIMAIRE_SLUG`) ou le NOM (semé
  depuis `OTO_BRAND_NAME`) n'est pas celui du tenant exporté : le refus donne les deux
  noms, l'import n'écrase pas le nom que l'instance déclare (décision du 28/09/2026) ;
- des secrets chiffrés sous une autre clé que la sienne (empreinte du manifeste ≠
  empreinte de `OTO_MCP_MASTER_KEY`), ou dont un ne se déchiffre pas sous l'AAD de sa
  ligne cible.

Ce qui change en chemin est `transformation.Transformation`, rien d'autre :

- **le tenant** : la ligne 1 semée par le démarrage prend les valeurs du tenant exporté,
  et toute clé vers `tenants(id)` vaut 1 ;
- **les comptes** perdent le préfixe `<slug>:` (validé le 28/09/2026) : toute VALEUR
  exactement égale à un sub du périmètre, ou à sa forme membre `<org>:<sub>`, est
  remplacée, à toute profondeur d'un JSON.

L'écriture se fait par lots (`TAILLE_LOT` lignes par aller-retour, `executemany`) : un
journal d'appels complet compte des centaines de milliers de lignes.

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
