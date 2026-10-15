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

- MCP : `oto_instance op=lend` (`to` = personne, `to_group` = équipe, `to_org` = l'org
  courante ; `account` ; `revoke`) ;
- REST : `POST /api/me/connectors/{connector}/lend`, même contrat ;
- dashboard : « Partager » sur la carte du compte, avec la liste de qui y a accès
  (`visible_to` de `GET /api/me/connector-instances`) et le retrait.

Une carte service (`outlook`, `gmail`…) prête le compte de son porteur
(`credential_of`), et la réponse le dit : prêter un compte prête tous les services qu'il
a autorisés.

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
désignation. La règle s'écrit **une fois**, sous `oto_mcp/access/`, et chaque chemin de
résolution s'y branche : Microsoft d'abord, puis la cascade (palier équipe nourri par
`group_store.lent_instances`, palier org).

## Invariants

- **On ne prête que sa propre ligne de coffre**, dans l'org courante (`SUB_ONLY`). Un
  prêt à une équipe ou à l'org reste dans l'org du prêteur ; un prêt à une personne
  peut la traverser.
- **Une cible qu'un connecteur ne sait pas servir est refusée à la pose**, nommément —
  jamais un `ok` inerte.
- **La garde des niveaux d'accès juge le propriétaire de la clé servie**
  (`connectors/access_level_gate.py`, `_judged_contexts`) : tout chemin qui sert la clé
  d'un autre propriétaire y est ajouté, sinon une équipe réglée en lecture se fait écrire
  dessus.
- **Une politique d'org qui interdit la clé perso** pour un connecteur interdit aussi
  d'en prêter une dans cette org.
- **Le prêt suit son propriétaire** : en pause, il est suspendu (refus nommé
  `lender_suspended`) ; quand le propriétaire quitte l'org, ses prêts nés dans cette org
  cessent, à une personne comme à une équipe.
- **La pose et le retrait se journalisent** : qui, quoi, vers qui, quand.

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
   de l'org, politique « clé perso interdite » lue par les prêts, journal.
4. Dashboard : « Partager » sur la carte du compte.
5. Microsoft connectable au nom de l'org ou d'une équipe.
6. Délégation des messageries ramenée au même geste (écran, outil), cible équipe à
   l'écran.
