# Partager un compte connecteur

Un seul geste, « partager », pour ouvrir un compte connecté (une boîte mail, un compte
Google, une clé d'API…) à une personne, à une équipe ou à toute l'org. Ce document dit le
modèle cible, la règle de service et ce que deviennent les mécanismes d'avant. Il se lit
avec `connector-vault.md` (le coffre, « Qui VOIT une instance ») et
`roles-and-resolution.md` (la cascade).

## Pourquoi

Six chemins permettaient d'ouvrir un compte à quelqu'un d'autre, nés un par un :

| Chemin | Couvre | Écran |
|---|---|---|
| Compte posé au palier org ou équipe à la connexion OAuth | Google, Salesforce, WordPress — pas Microsoft | oui (« Autoriser » côté org/équipe) |
| Clé d'org ou d'équipe | connecteurs `org_shareable` | oui |
| Prêt d'instance (`share_side`, ADR 0044) | tous, Microsoft depuis le 09/10 | **non**, agent seulement |
| Délégation « opérer mon compte » (#55) | messageries Unipile | oui, personnes seulement |
| Héritage de clés par partage de projet (#480) | les paliers du partageur | non |
| Accès à une clé plateforme (`share_down`, grants) | clés d'oto | super_admin |

Les deux derniers ne partagent pas un compte d'une personne (un projet, une clé d'oto) :
**hors périmètre**. Les quatre premiers se recouvrent (ouvrir à une équipe : trois façons ;
à une personne : deux), n'ont pas tous d'écran, et ne suivent pas la même règle quand le
compte d'un autre sert un appel.

## Le modèle : deux concepts, pas plus

**1. Le compte de l'org ou de l'équipe.** Posé par l'admin (org) ou le chef d'équipe,
il appartient à l'org ou à l'équipe : il survit au départ de qui l'a branché, il sert
ses membres **sans désignation**, comme aujourd'hui (palier équipe puis org de la
cascade). Clés d'API et comptes OAuth connectés « au nom de » l'org ou de l'équipe.

**2. Le compte partagé par une personne** — le *prêt*. Il reste à son propriétaire :
lui seul le partage et le reprend, il s'éteint avec lui. Un seul stockage, `share_side`
sur la ligne de coffre du propriétaire (`user:<sub>`, `group:<id>`, `org:<id>`), et un
seul geste :

- MCP : `oto_instance op=lend` (`to` = personne, `to_group` = équipe, `to_org=true` = l'org
  courante, exactement un des trois ; `account` ; `revoke`) ;
- REST : `POST /api/me/connectors/{connector}/lend`, même contrat ; la réponse dit l'état
  complet après le geste (`lent_to`, `lent_to_groups`, `lent_to_org`) ;
- dashboard : « Partager » sur la carte du compte, avec la liste de qui y a accès
  (`visible_to` de `GET /api/me/connector-instances`) et le retrait.

Une carte service (`outlook`, `gmail`…) prête le compte de son porteur
(`credential_of`), mais **pas tous ses services** (décision du 16/10/2026, signal 1399) :
voir « Les services prêtés » ci-dessous.

## Les services prêtés (décision du 16/10/2026)

Un compte Google ou Microsoft autorise ses services un par un (Gmail, Drive, Google Ads,
Outlook…), sur la même ligne de coffre. Le prêter prêtait la ligne, donc **tous** ses
services, et chaque service que le prêteur autorisait ensuite devenait accessible à
l'emprunteur, sans nouvel avertissement. On ne pouvait pas prêter Google Ads seul.

**Le prêt d'un compte Google ou Microsoft retient la liste des services prêtés**, par
emprunteur (`connector_credentials.share_services`, `{cible: [services]}`,
`access/prets.PORTEURS_A_SERVICES`) :

- à la pose, `services` donné, sinon la carte nommée (`connector='google_ads'`), sinon les
  services que le compte a autorisés **ce jour-là** — chacun doit être un service du compte
  qu'il a autorisé (`connectors/services_accordes`) ; la réponse dit la liste (`services`) ;
- **un service autorisé plus tard n'est jamais prêté implicitement** ; le prêteur étend ou
  réduit en prêtant à nouveau au même emprunteur avec `services` (la liste remplace la
  précédente, et le geste se journalise comme une pose) ;
- à l'usage, au résolveur unique (`access/resolve.resolve_credential` →
  `prets.exiger_service_prete`), un service hors de la liste est refusé
  `service_not_lent`, en nommant le prêteur, les services couverts et le geste qui étend ;
  les services prêtés à une personne sont l'union des listes de toutes les cibles qui
  l'atteignent (elle, ses équipes, son org) ; **une cible sans liste n'a aucun service
  prêté**, jamais « tous » ;
- un appel au compte lui-même (`google`, `microsoft` : l'identité seule) n'est pas un
  service et n'est pas filtré ; une clé sans services propres (Unipile, une clé d'API) se
  prête entière, et la réponse le dit.

Les prêts posés avant cette règle ont été **figés** par la révision
`0060_services_pretes` : chaque cible a reçu les services que le compte avait autorisés au
moment de la révision. Rien n'a cassé, rien ne s'élargit ensuite.

## La règle de service (décision du 15/10/2026)

**Un compte prêté par une autre personne ne sert que désigné explicitement**
(`_account=`, ou `_instance=`), quel que soit le connecteur et quelle que soit la cible du
prêt (personne, équipe, org). Sans désignation, l'appel est refusé `account_required`, et
le refus **nomme** les comptes prêtés et leur propriétaire : l'agent choisit, puis rappelle.
La réponse d'un appel servi par un prêt dit le compte et `lent_by`.

*Pourquoi* : le 14/10, une personne sans compte propre a écrit dans la boîte de la
collègue qui la lui avait prêtée, sans le savoir. Désigner coûte un aller-retour la première
fois ; écrire au nom de quelqu'un par surprise ne se rattrape pas.

Les comptes du concept 1 (ceux de l'org ou de l'équipe elles-mêmes) restent servis sans
désignation. La règle s'écrit **une fois**, sous `oto_mcp/access/` (`prets.py`), et chaque
chemin de résolution s'y branche : Microsoft (`auth/microsoft._lent_for_call`, nourri par
`prets.prets_de` — personne, équipe, org), puis la cascade — paliers équipe et org, nourris
par `group_store.lent_instances` et `live_loans`, décidés dans leur sonde
(`PretsDuParcours.au_palier_equipe`, `au_palier_org`). Dans la cascade, sans désignation,
le compte de l'équipe elle-même sert, sinon celui de l'org ; si seuls des prêts auraient
servi, le refus les nomme (avec « via the team … » ou « to the whole org … »), jamais la
clé plateforme à leur place. Un prêt à l'org ne sert que les membres de cette org, la ligne
du prêteur exceptée ; l'écho dit `lent_by` et `lent_via_org`.

*Ce qui change pour qui s'en servait* : un membre d'équipe qui utilisait un prêt à son
équipe sans le nommer reçoit `account_required` à son prochain appel, et le rejoue avec
`_account=` ; une équipe qui a son propre compte ET un prêt sert désormais le sien, au lieu
d'un refus d'ambiguïté.

## Invariants

- **On ne prête que sa propre ligne de coffre**, dans l'org courante (`SUB_ONLY`). Un
  prêt à une équipe ou à l'org reste dans l'org du prêteur ; un prêt à une personne
  peut la traverser.
- **Une cible qu'un connecteur ne sait pas servir est refusée à la pose**, nommément —
  jamais un `ok` inerte.
- **La garde des niveaux d'accès juge le propriétaire de la clé servie**
  (`connectors/access_level_gate.py`, `_judged_contexts`) : tout chemin qui sert la clé
  d'un autre propriétaire y est ajouté, sinon une équipe réglée en lecture se fait écrire
  dessus. Un prêt ne servant que désigné, la désignation (`_instance=`, `_account=`) suffit
  à juger, avant la résolution, l'org du prêteur et l'équipe emprunteuse (`_loan_contexts`).
- **Une politique d'org qui interdit la clé perso** pour un connecteur interdit aussi
  d'en prêter une dans cette org : refus `personal_key_forbidden` à la pose ; un prêt posé
  avant la politique cesse de servir (prêt à une équipe : `lent_instances` l'ignore ; prêt
  épinglé : refus de l'instance perso désignée).
- **Le prêt suit son propriétaire** : en pause, il est suspendu (refus nommé
  `lender_suspended`) ; quand le propriétaire quitte l'org, ses prêts nés dans cette org
  cessent, à une personne comme à une équipe (refus nommé `lender_left_org` à l'épinglage,
  prêt à une équipe ignoré) — lu à chaque appel, rien n'est détaché du coffre.
- **La pose et le retrait se journalisent** : qui, quoi, vers qui, quand
  (`connector_loan_events`, révision `0058_journal_des_prets`), dans la transaction qui
  écrit le `share_side` ; seul un geste qui change l'état y entre, jamais un secret.
  L'admin d'une org lit les prêts actifs de son org (`oto_org op=list_loans`,
  `GET /api/orgs/{id}/loans`) : prêteur, compte, emprunteur, services, date de la dernière
  pose journalisée (`since: null` pour un prêt antérieur au journal). La lecture par le
  propriétaire reste à exposer (lot 4, avec l'écran).

## Ce que deviennent les chemins d'avant

| Chemin | Devient |
|---|---|
| Compte posé au palier org/équipe à la connexion | Concept 1, inchangé ; **ouvert à Microsoft** (lot 5) |
| Clé d'org ou d'équipe | Concept 1, inchangé |
| Prêt d'instance | Le geste unique (concept 2), étendu à l'org, avec écran |
| Délégation des messageries | Même geste à l'écran et dans l'agent ; stockage propre conservé, car le compte opéré n'est pas une ligne de coffre (le bénéficiaire l'atteint avec sa propre clé Unipile) |

## Lots

1. Ce document.
2. Règle de service partagée et prêts Microsoft à une équipe — lot de la session Microsoft.
3. Backend : cible `to_org`, règle de service étendue à la cascade, fin du prêt au départ
   de l'org, politique « clé perso interdite » lue par les prêts, journal. *3a* : la
   cascade (palier équipe), le départ, la politique, la garde des niveaux d'accès. *3b* :
   `to_org` (palier org, Microsoft, listing) et le journal, écrit ; sa lecture reste à
   exposer.
4. Dashboard : « Partager » sur la carte du compte.
5. Microsoft connectable au nom de l'org ou d'une équipe.
6. Délégation des messageries ramenée au même geste (écran, outil), cible équipe à
   l'écran.
7. Services prêtés (signal 1399). *7a*, backend : la liste par emprunteur, figée à la pose,
   exigée à l'usage, révision `0060_services_pretes` qui fige l'existant. *7b*, console :
   choisir les services au partage, les afficher sur la carte du compte et dans la liste des
   prêts de l'org, étendre ou réduire d'un geste.
