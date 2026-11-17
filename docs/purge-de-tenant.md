---
title: Purge d'un tenant parti sur sa propre instance
type: procédure
description: >-
  Effacer de l'instance partagée un tenant tiers dont le périmètre a été transféré sur
  sa propre instance (`docs/export-perimetre.md`) : la base, le journal, les archives
  froides du journal et les objets du stockage (versions comprises), en un seul
  passage, sur un seul go, avec un constat final par élément. Plus deux gestes hors
  script : l'annuaire du tenant et les sauvegardes. Commande `scripts/purger_tenant.py`.
---

# Purge d'un tenant parti sur sa propre instance

C'est l'export par périmètre (`docs/export-perimetre.md`) joué à l'envers. Il reprend
le même classement (`export_perimetre/classement.py`, une règle par table) et le même
périmètre (`perimetre.resoudre` sur les orgs du tenant, avec ses refus). Les comptes du
tenant sans appartenance (`<slug>:x` d'anciens membres) en font partie ; s'ils sont
membres d'une org hors périmètre, la purge refuse (`ComptesHorsTenant`). Le code vit
dans `oto_mcp/export_perimetre/` :

- `purge.py` : le périmètre figé, la base, le journal, la règle du journal ;
- `purge_archives.py` : les archives du journal ;
- `purge_objets.py` : le stockage ;
- `purge_remise.py` : la remise au titulaire.

La commande est `scripts/purger_tenant.py`. Seul `tout` écrit ; `base`, `objets` et
`archives` ne font que compter, et `extraire` prépare la remise.

## Ce qui part, ce qui reste

| élément | ce qui part | ce qui reste, et pourquoi |
|---|---|---|
| 1. base | Les lignes du périmètre dans toute table `possedee`, `indirecte` ou `exclue`, avec les dérivés du journal par org (`journal_totaux_jour`, `journal_jobs_jour`), la ligne du tenant et ses comptes. | **Conservées** (`purge.CONSERVEES`) : nos pièces, c'est-à-dire les acceptations de nos documents légaux et les tables de commerce et de facturation (voir plus bas). **Non touchées** : les tables `instance` sans propriétaire. |
| 2. journal | Les appels du périmètre (`tool_calls`), selon `purge.JOURNAL_REGLE` : une org du périmètre, ou un compte du tenant sans org. Les appels de nos administrateurs dans une org du tenant partent avec elle (leurs arguments portent des données du tenant) ; ils sont comptés à part (« dont N appels de support »). | Les appels d'un compte du tenant dans une org voisine, et ceux d'un voisin dont le compte effectif est du tenant : ils sont au voisin. |
| 3. archives du journal | Dans chaque mois inscrit au registre `journal_archives`, quelle que soit sa date, EXACTEMENT les enregistrements que la règle du journal désigne (la même règle, compilée en Python). L'archive est réécrite sous la même clé ; la nouvelle version est relue par son versionId, puis l'ancienne est supprimée par le sien. | Les enregistrements des autres, octet pour octet. Un mois où la part du tenant vaut 0 n'est pas réécrit. |
| 4. objets | Toutes les versions (et marqueurs de suppression) sous les préfixes de ses propriétaires, chacune supprimée par son versionId, `null` compris : `avatars/<sub>/`, `images/<sub>/`, `copilot-attachments/<sub>/`, `org-logos/<org>/`, `project-files/<projet>/`, `transcription-jobs/<projet>/`, `app-files/<app>/`. Le `/` final est exigé. | Les clés que la base désigne HORS de ces préfixes : comptées, jamais supprimées, à examiner. Les dépôts de transit (`tmp/`) expirent seuls en un jour. |
| 5. annuaire | **Hors script** : la base de comptes de connexion du tenant, avec d'autres identifiants et une autre base. | — |
| 6. sauvegardes | **Hors script** : les sauvegardes de la base antérieures au passage gardent tout, jusqu'à leur expiration. | jusqu'à leur échéance |

Les tables partagées par plusieurs propriétaires, comme le journal `tool_calls`, les
disponibilités de connecteurs, ou les nœuds et guides de plateforme, ne sont **jamais
vidées**. Chaque `DELETE` porte le prédicat de la ligne. Il n'y a jamais de `TRUNCATE`.

**La facturation.** La conserver est impossible quand elle existe : les tables de
facturation désignent `orgs` en `ON DELETE CASCADE` (par exemple `spend_blocks`,
`oto_mcp/db/schema/credits.py:122`), et supprimer l'org les emporterait. La purge
refuse donc (`reference_entrante`) un tenant qui a de la facturation : c'est attendu.
Les soldes de crédit (`credit_balances`, payeur `org:<id>` en texte, sans clé) sont
classés `instance` et restent. Conserver la facturation d'un tenant qui en a est un
sujet à part, à trancher avant sa purge.

## Les refus (code 2, rien d'écrit)

- **Les refus du périmètre de l'export** :
  - un compte du tenant est aussi membre d'une org hors périmètre ;
  - un membre d'une org du tenant n'est pas un compte du tenant ;
  - le tenant héberge aussi d'autres orgs.
- `tenant_primaire` : le tenant est la ligne 1 de l'instance.
- `releve_perime` : le périmètre a changé depuis le compte à blanc (une org, une équipe ou un compte en plus ou en moins).
- `partage_vers_tiers` :
  - une ressource du tenant est partagée à une org, une équipe ou un compte hors du tenant, ou à un destinataire sans type ;
  - un compte de messagerie du tenant est prêté à un compte ou une équipe hors du tenant (`connector_account_grants`, `connector_account_group_grants`).

  Un partage reçu d'un tiers part avec son destinataire.
- `copie_par_un_tiers` : une entrée de la bibliothèque publique a été écrite par une autre org à partir d'une org du tenant.
- `reference_entrante` : une ligne qui reste désigne par clé étrangère une ligne supprimée (la facturation, ci-dessus).
- `ArchiveDiscordante` :
  - une archive ne se relit pas au compte inscrit au registre ;
  - une archive touchée n'a pas de filet : version courante `null`, gestion de versions jamais activée, plusieurs versions ou un marqueur de suppression ;
  - la nouvelle version d'une archive ne se relit pas juste (l'ancienne est gardée) ;
  - une extraction déjà là est incomplète.

## Le passage, dans l'ordre

| # | geste | qui | commande |
|---|---|---|---|
| 0 | **Prérequis** : l'accord écrit du titulaire, la comparaison des comptes des deux instances (« tout a été copié »), et le go. | l'opérateur | — |
| 1 | Remise au titulaire, **s'il la demande** : son journal et les lignes écrites après le transfert. Elle lit du contenu, donc demande une décision. Jamais un secret, même chiffré ou haché. | le lanceur de prod | `purger_tenant.py extraire --tenant <slug> --dossier R --depuis <instant du transfert>` |
| 2 | Le compte à blanc. Il **fige le périmètre** dans `P/etat.json` : journal par mois, part du tenant dans chaque archive, objets. | le lanceur de prod | `purger_tenant.py tout --tenant <slug> --dossier P` |
| 3 | Le passage, sur le **même** dossier. | le lanceur de prod | `purger_tenant.py tout --tenant <slug> --dossier P --appliquer [--extraction-faite E]` |
| 4 | L'annuaire du tenant : supprimer sa base, dans le même créneau. | l'infrastructure | — |
| 5 | Vérifier que les sauvegardes antérieures expirent, et retirer tout dump manuel qui contient la base. | l'infrastructure | — |
| 6 | Le constat (`P/constat.json`) remis au titulaire, avec les étapes 4 et 5 faites. | l'opérateur | — |

Le compte à blanc tourne dans une transaction `READ ONLY` explicite, et ne prend pas le
verrou de l'archivage quotidien. Le passage enchaîne :

1. **Vérification du relevé** : tant que le tenant existe, son périmètre frais doit
   égaler le périmètre figé, sinon `releve_perime`.
2. **Base** (`purge.appliquer`), une transaction `REPEATABLE READ`, tout SAUF le journal.
   - Le relevé est revérifié, puis les refus et les comptes sont rejoués dans
     l'instantané qui écrit.
   - Les tables sont vidées enfants d'abord, et chaque suppression doit égaler son compte.
   - Le résidu, recompté, doit valoir 0. Alors seulement, le COMMIT.

   **Un refus ici laisse TOUT intact**, journal, archives et objets compris : rien n'a
   encore été écrit.
3. **Journal** (`purge.vider_journal`), depuis le périmètre figé, par tranches de 50 000
   ids, une courte transaction par tranche. Chaque tranche porte le prédicat, donc se
   rejoue. Le journal est recompté à 0 (jusqu'à trois passes). `tool_calls` n'a pas de
   clé étrangère : rien n'empêche cet ordre.
4. **Archives**, sous le verrou de l'archivage quotidien (`deploy/archive_tool_calls.py`,
   `LOCK_KEY`) :
   - chaque archive touchée doit avoir UNE version, au versionId réel (pas `null`), sans
     marqueur de suppression, et la gestion de versions doit être `Enabled` ou
     `Suspended` : en `Suspended`, l'écriture crée la version `null` et laisse vivre la
     version au versionId réel, qui sert de filet. Tout autre cas est refusé avant toute
     écriture ; le compte à blanc dit, par archive, la version courante, le verdict et
     les lignes à retirer ;
   - la part du tenant est extraite dans `P/journal-archive/`, ou dans
     `--extraction-faite`. Une extraction complète déjà là est reprise ;
   - chaque mois concerné est réécrit ; la NOUVELLE version, lue par son versionId, est
     recomptée (part 0, reste égal au compte) ;
   - le registre est mis à jour, puis l'ANCIENNE version est supprimée par son versionId :
     sans cela, la purge n'est pas finie, elle porte encore le tenant ;
   - si la nouvelle version ne se relit pas juste, l'ancienne est gardée : la nouvelle est
     retirée par son versionId si l'ancienne est relue présente (elle redevient
     courante), sinon le refus nomme les deux versionId et le geste à faire.
5. **Objets** : toutes les versions sous les préfixes figés sont supprimées, chacune par
   son versionId (une suppression sans versionId ne poserait qu'un marqueur).
6. **Recompte**, dans une nouvelle transaction `READ ONLY`, depuis le périmètre figé :
   - toutes les tables purgées, la ligne du tenant et les comptes à son préfixe ;
   - l'inventaire des objets ;
   - les archives.

   Il alimente le **constat** (`P/constat.json`, et en texte) : par élément, ce qui était
   là, ce qui a été supprimé, ce qui reste (attendu 0), ce qui est conservé et pourquoi,
   les appels de support, et les deux gestes hors script. Le code de sortie est 0 si
   tout reste vaut 0, 1 sinon.

**Reprise.** `P/etat.json` dit les étapes faites. Une étape qui échoue arrête tout et s'y
inscrit (`echec` : l'étape, l'erreur, les étapes faites). Rejoué sur le même dossier, le
passage saute ce qui est fait et reprend là. Pour les archives :

- une panne entre le dépôt d'une archive et la mise à jour du registre est reconnue à la
  relecture : l'objet courant est déjà la version purgée (part 0, total égal à
  l'inscrit moins la part extraite). Seuls le registre et les versions anciennes (par
  leur versionId) sont alors traités ;
- une panne avant la suppression des versions anciennes est reprise de même.

**Le volume.** Sur une base jetable, 1,5 M lignes de journal sur 4,2 M sont parties en
3,8 s par lots de 50 000 ids, et la transaction de la base a pris 1,8 s. Sur la prod,
avec des lignes plus lourdes et sous charge, il faut compter de l'ordre de la minute pour
le journal, avec des verrous de ligne seulement. Aucune transaction longue ne retient le
nettoyage de la base. Un créneau creux suffit.

## Le constat attendu

```
base     : N ligne(s) avant (dont S appels de support), N supprimée(s), 0 restante(s) ; conservé : {legal_acceptances: …}
objets   : V version(s) avant, V supprimée(s), 0 restante(s) ; H clé(s) hors préfixe non touchée(s)
archives : A ligne(s) du tenant avant, 0 restante(s)
  AAAA-MM : R ligne(s) retirée(s) ; ancienne version supprimée <versionId> ; nouvelle version <versionId>
annuaire : À FAIRE HORS SCRIPT — …
sauvegardes: À FAIRE HORS SCRIPT — …
COMPLET
```
