## prerequisite — une clé API OpenRouter, posée par la plateforme sur le tenant

jev est un modèle de **TypeSafe**, servi par **OpenRouter** : la clé attendue est donc une [clé API OpenRouter](https://openrouter.ai/settings/keys) (elle commence par `sk-or-`), pas une clé TypeSafe — il n'y en a pas. c'est **la plateforme** qui la dépose pour ses organisations : un administrateur du tenant la pose une fois, et toutes ses orgs décident dessus.
- réserve-lui une clé dédiée, avec son propre plafond de dépense : la même clé, ailleurs, ouvrirait tout le catalogue de modèles d'openrouter
- une clé posée au niveau d'une organisation n'est **pas** servie : l'outil la refuse en le disant, plutôt que de faire payer un tiers en silence
- sans clé déposée, aucun appel ne part — il n'y a pas de palier gratuit sur ce connecteur

## usage — trancher une question fermée sans dépenser un tour de modèle

jev rend une réponse **typée avec sa probabilité**, jamais du texte : oui/non (`noul`), un choix parmi des options (`choice`), une position sur une échelle ordonnée (`score`). le code branche dessus directement.
- « cette ligne correspond-elle à ma cible ? » → `jev_ask` avec l'état de la ligne et un `noul`
- « qualifie ces 200 profils avant que je les enrichisse » → `jev_items` : une grille, N états, rien n'est écrit
- pose **toute la grille en un appel** : les questions d'un même appel sont répondues en parallèle, l'état n'est facturé qu'une fois et chaque question de plus coûte ~48 jetons
- deux questions d'un même appel ne se voient pas : un enchaînement demande deux appels

## note — lire une probabilité, et où placer le seuil

une probabilité proche de 0,5 veut dire « aussi probable que l'inverse », jamais « moyen ».
- garde **deux** seuils et laisse la bande du milieu à une relecture humaine ou à un modèle de texte : c'est moins cher que de forcer une étiquette
- `confidence` décrit la concentration de la distribution, pas la sûreté du geste qui suit
- un `choice` répond TOUJOURS, même quand la question ne se pose pas : sur un contact hors cible, la persona rendue ne veut rien dire (mesuré le 28/09). lis d'abord le `noul` qui filtre, et le `choice` seulement s'il passe — ou prévois une option « aucune » et regarde sa probabilité
- la réponse nomme le `model` exact qui a servi : garde-le à côté de la décision, c'est ce qui rend un seuil reproductible

## note — coût

l'**entrée seule** est facturée, la sortie est gratuite, et chaque réponse porte `usage.cost`, le coût réel en dollars — c'est lui qui est relevé, pas un nombre d'appels.
- un état court (une ligne de table, un profil) coûte quelques dizaines de micro-dollars
- l'état est plafonné à 32 000 jetons : n'envoie que les champs qui servent au jugement, pas la fiche entière
