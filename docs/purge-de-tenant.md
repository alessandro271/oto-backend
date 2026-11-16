---
title: Purge d'un tenant parti sur sa propre instance
type: procédure
description: >-
  Effacer de l'instance partagée un tenant tiers dont le périmètre a été transféré sur
  sa propre instance (`docs/export-perimetre.md`) : la base, les objets du stockage
  (versions comprises), les archives froides du journal, en un seul passage, sur un
  seul go, avec un constat final par élément. Plus deux gestes hors script :
  l'annuaire du tenant et les sauvegardes. Commande `scripts/purger_tenant.py`.
---

# Purge d'un tenant parti sur sa propre instance

C'est l'export par périmètre (`docs/export-perimetre.md`) joué à l'envers. Il reprend
le même classement (`export_perimetre/classement.py`, une règle par table) et le même
périmètre (`perimetre.resoudre` sur les orgs du tenant, avec ses refus). Le code vit
dans `oto_mcp/export_perimetre/` :

- `purge.py` : la base ;
- `purge_objets.py` : le stockage ;
- `purge_archives.py` : les archives du journal ;
- `purge_remise.py` : la remise au titulaire.

La commande est `scripts/purger_tenant.py`.

## Ce qui part, ce qui reste

| élément | ce qui part | ce qui reste, et pourquoi |
|---|---|---|
| 1. base | Les lignes du périmètre dans toute table `possedee`, `indirecte` ou `exclue`, avec les dérivés du journal par org (`journal_totaux_jour`, `journal_jobs_jour`), la ligne du tenant et ses comptes. | **Conservées** (`purge.CONSERVEES`) : nos pièces, c'est-à-dire les acceptations de nos documents légaux et les tables de commerce et de facturation. Elles sont comptées à part et exclues de la vérification de fin. **Non touchées** : les tables `instance` sans propriétaire. |
| 2. objets | Toutes les versions des objets que la base désigne : fichiers de projet et d'app, audios, pièces jointes, avatars et logos cités par URL. S'y ajoute le préfixe `images/<sub>/` de chaque compte. | rien |
| 3. archives du journal | Les enregistrements du tenant dans chaque mois archivé : `sub` ou `effective_sub` en `<slug>:`, ou `org_id` du périmètre. L'archive est réécrite sous la même clé, et ses versions non courantes sont supprimées. | les enregistrements des autres propriétaires, octet pour octet |
| 4. annuaire | **Hors script** : la base de comptes de connexion du tenant, avec d'autres identifiants et une autre base. | — |
| 5. sauvegardes | **Hors script** : les sauvegardes de la base antérieures au passage gardent tout, jusqu'à leur expiration. | jusqu'à leur échéance |

Les tables partagées par plusieurs propriétaires, comme le journal `tool_calls`, les
disponibilités de connecteurs, ou les nœuds et guides de plateforme, ne sont **jamais
vidées**. Chaque `DELETE` porte le prédicat de la ligne. Il n'y a jamais de `TRUNCATE`.

## Les refus (code 2, rien d'écrit)

- **Les refus du périmètre de l'export** :
  - un compte du tenant est aussi membre d'une org hors périmètre ;
  - un membre d'une org du tenant n'est pas un compte du tenant ;
  - le tenant héberge aussi d'autres orgs.
- `tenant_primaire` : le tenant est la ligne 1 de l'instance.
- `partage_vers_tiers` : une ressource du tenant est partagée à une org, une équipe ou un compte hors du tenant. Un partage reçu d'un tiers part avec son destinataire.
- `copie_par_un_tiers` : une entrée de la bibliothèque publique a été écrite par une autre org à partir d'une org du tenant.
- `reference_entrante` : une ligne qui reste désigne par clé étrangère une ligne supprimée. Par exemple, une pièce comptable conservée qu'un `ON DELETE CASCADE` depuis `orgs` emporterait : la régler à la main d'abord.
- `ArchiveDiscordante` : une archive ne se relit pas au compte inscrit au registre.

## Le passage, dans l'ordre

| # | geste | qui | commande |
|---|---|---|---|
| 0 | **Prérequis** : l'accord écrit du titulaire, la comparaison des comptes des deux instances (« tout a été copié »), et le go. | l'opérateur | — |
| 1 | Remise au titulaire, **s'il la demande** : son journal et les lignes écrites après le transfert. Elle lit du contenu, donc demande une décision. | le lanceur de prod | `purger_tenant.py extraire --tenant <slug> --dossier R --depuis <instant du transfert>` |
| 2 | Le compte à blanc des éléments 1 à 3 (journal par mois, archives par mois). | le lanceur de prod | `purger_tenant.py tout --tenant <slug>` |
| 3 | Le passage. | le lanceur de prod | `purger_tenant.py tout --tenant <slug> --dossier P --appliquer [--extraction-faite R/journal-archive]` |
| 4 | L'annuaire du tenant : supprimer sa base, dans le même créneau. | l'infrastructure | — |
| 5 | Vérifier que les sauvegardes antérieures expirent, et retirer tout dump manuel qui contient la base. | l'infrastructure | — |
| 6 | Le constat (`P/constat.json`) remis au titulaire, avec les étapes 4 et 5 faites. | l'opérateur | — |

Le passage de l'étape 3 enchaîne les gestes suivants, sous le verrou de l'archivage
quotidien du journal (`deploy/archive_tool_calls.py`, `LOCK_KEY`) :

1. **Relevé** (`P/perimetre.json`) : le périmètre et les objets, relevés une fois AVANT
   toute écriture. Après la purge de la base, plus rien ne les désigne.
2. **Archives** :
   - la part du tenant est extraite dans `P/journal-archive/` (ou vérifiée dans
     `--extraction-faite`) ;
   - chaque mois est réécrit, relu depuis le stockage et recompté (part du tenant 0,
     reste égal au compte à blanc) ;
   - le registre est mis à jour ;
   - les versions non courantes sont supprimées.
3. **Journal, par lots** (`purge.vider_par_lots`). Les refus sont joués d'abord : un
   tenant refusé ne perd rien. Le journal du périmètre (`tool_calls`, plus d'un million
   de lignes) part par tranches de 50 000 ids, une courte transaction par tranche. Chaque
   tranche porte le prédicat, ce qui la rend idempotente.
4. **Base** : une transaction `REPEATABLE READ`.
   - Les refus et les comptes sont rejoués dans l'instantané qui écrit.
   - Les tables sont vidées enfants d'abord, avec le reste du journal écrit pendant les
     lots, et chaque suppression doit égaler son compte.
   - Le résidu, recompté, doit valoir 0.
   - Alors seulement, le COMMIT.
5. **Objets** : toutes les versions sont supprimées, puis l'inventaire est relu, et doit
   être vide.
6. **Constat** (`P/constat.json`, et en texte) : par élément, ce qui était là, ce qui a
   été supprimé, ce qui reste (attendu 0), ce qui est conservé et pourquoi. Il rappelle
   les deux gestes hors script. Le code de sortie est 0 si tout reste vaut 0, 1 sinon.

**Reprise.** `P/etat.json` dit les étapes faites. Une étape qui échoue arrête tout et s'y
inscrit (`echec`, étape et erreur). Rejoué sur le même `--dossier`, le passage saute ce
qui est fait et reprend là. Chaque étape est en outre idempotente : rejouée, elle ne
trouve plus rien.

**Le volume et le principe « COMMIT seulement si tout vaut 0 ».** Le journal sort de la
transaction finale, parce que supprimer un million de lignes d'un coup ferait une
transaction longue : elle retiendrait le nettoyage de toute la base et pousserait le WAL
d'un bloc. Le principe vaut donc pour la transaction finale, et pour elle seule. Le
journal est déjà vidé quand elle s'ouvre, et ses lots sont irréversibles. Trois garanties
les encadrent :
- les lots ne partent qu'après les refus ;
- la part des archives a été extraite avant eux ;
- la transaction finale recompte le journal à 0, comme toutes les autres tables.

Si elle refuse ensuite, par exemple à cause d'un partage créé entre-temps, le journal
reste supprimé, et le reste attend une nouvelle passe.

Mesure sur une base jetable : 1,5 M lignes du tenant parmi 4,2 M ont été supprimées en
3,8 s par lots de 50 000 ids, et la transaction finale a pris 1,8 s. Sur la prod, avec
des lignes plus lourdes et sous charge, il faut compter de l'ordre de la minute, avec
des verrous de ligne seulement. Un créneau creux suffit.

**Mois par mois.** Le compte à blanc ventile le journal du périmètre encore en base par
mois calendaire (`journal_par_mois`). Les archives sont parcourues TOUTES, telles que
`journal_archives` les inscrit, quelle que soit leur date : pour chaque mois, le total,
la part du tenant et le reste. Un mois où la part vaut 0 n'est pas réécrit. Une purge
jouée après l'archivage d'un nouveau mois le traite donc comme les autres.

## Le constat attendu

```
base     : N ligne(s) avant, N supprimée(s), 0 restante(s) ; conservé : {legal_acceptances: …}
objets   : V version(s) avant, V supprimée(s), 0 restante(s)
archives : A ligne(s) du tenant avant, 0 restante(s)
annuaire : À FAIRE HORS SCRIPT — …
sauvegardes: À FAIRE HORS SCRIPT — …
COMPLET
```
