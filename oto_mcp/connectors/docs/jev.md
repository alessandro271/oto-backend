## prerequisite — une clé API OpenRouter, posée par le tenant

jev est un modèle de **TypeSafe**, servi par **OpenRouter** : la clé attendue est donc une [clé API OpenRouter](https://openrouter.ai/settings/keys) (elle commence par `sk-or-`), pas une clé TypeSafe — il n'y en a pas. c'est **le tenant** qui la dépose pour ses organisations : un de ses administrateurs la pose une fois, et toutes ses orgs décident dessus. oto ne fournit pas de clé plateforme jev.
- réserve-lui une clé dédiée, avec son propre plafond de dépense : la même clé, ailleurs, ouvrirait tout le catalogue de modèles d'openrouter
- une clé posée au niveau d'une organisation, d'une équipe ou d'une personne n'est **pas** servie, et elle masque celle du tenant : l'outil la refuse en le disant et en nommant qui doit la retirer
- sans clé déposée, aucun appel ne part — il n'y a pas de palier gratuit sur ce connecteur
- l'état envoyé part chez un **tiers** (OpenRouter, qui le passe à TypeSafe) : c'est une sous-traitance du tenant qui dépose la clé

## usage — trier en lot avec une même grille

jev rend une réponse **typée avec sa probabilité**, jamais du texte : oui/non (`noul`), un choix parmi des options (`choice`), une position sur une échelle ordonnée (`score`). le code branche dessus directement. il paie sur les **lots** : un cas isolé que l'agent sait trancher lui-même n'a pas besoin de jev.
- « qualifie ces 200 profils avant que je les enrichisse » → `jev_items` : une grille, N états, rien n'est écrit ; un lot qui manque de temps (~40 s) rend ce qui est décidé et, dans `retry`, les états à renvoyer
- « cette ligne correspond-elle à ma cible ? », une grille à caler avant un lot → `jev_ask` avec l'état de la ligne et un `noul`
- un état tient en **16 Ko** de JSON : les champs qui servent au jugement, jamais la fiche ou le document entier
- pose **toute la grille en un appel** : les questions d'un même appel sont répondues en parallèle, l'état n'est facturé qu'une fois et chaque question de plus coûte ~48 jetons
- deux questions d'un même appel ne se voient pas : un enchaînement demande deux appels

## note — lire une probabilité, et où placer le seuil

une probabilité proche de 0,5 veut dire « aussi probable que l'inverse », jamais « moyen ».
- garde **deux** seuils et laisse la bande du milieu à une relecture humaine ou à un modèle de texte : c'est moins cher que de forcer une étiquette
- `confidence` décrit la concentration de la distribution, pas la sûreté du geste qui suit
- un `choice` répond TOUJOURS, même quand la question ne se pose pas : sur un contact hors cible, la persona rendue ne veut rien dire. lis d'abord le `noul` qui filtre, et le `choice` seulement s'il passe — ou prévois une option « aucune » et regarde sa probabilité
- la réponse nomme le `model` exact qui a servi : garde-le à côté de la décision, c'est ce qui rend un seuil reproductible

## note — coût

l'**entrée seule** est facturée, la sortie est gratuite, et chaque réponse porte `usage.cost`, le coût réel en dollars — c'est lui qui est relevé, pas un nombre d'appels.
- un état court (une ligne de table, un profil) coûte quelques dizaines de micro-dollars
- un état est plafonné à 16 Ko de JSON (le contexte de l'amont va jusqu'à 32 000 jetons) : n'envoie que les champs qui servent au jugement, pas la fiche entière
