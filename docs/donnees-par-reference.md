---
title: Données par référence — ce qui ne passe plus par les arguments d'outil
type: explanation
description: >-
  Pourquoi et comment un geste de prospection ne porte plus ni URL extérieure ni donnée
  de personne dans ses arguments : le receveur des téléphones Apollo (URL générée par
  oto, jeton par commande, stockage 30 jours, lecture oto d'abord puis Apollo en repli,
  `webhook_url` retiré du schéma mais accepté). À lire avant d'ajouter un receveur ou
  une écriture par référence.
---

# Données par référence

Un client MCP peut examiner chaque appel d'outil avant de l'envoyer — et en refuser un
qui transporte une donnée de personne (email, téléphone, nom) ou une adresse hors de
l'environnement de l'utilisateur. Un accord donné dans la conversation ne lève pas ce
refus. Deux gestes courants de prospection tombaient dedans : le reveal de téléphone
Apollo (une `webhook_url` fournie par l'appelant) et la création de leads/contacts (la
fiche de la personne en arguments, une fois par ligne).

**La règle : la donnée cesse réellement de passer par les arguments.** Ni encodage, ni
renommage de champ, ni valeur découpée — c'est le SERVEUR qui la déplace. L'agent
désigne (un tableau, des lignes, un `request_id`) ; oto lit, appelle, écrit en retour.

## 1. Le receveur des téléphones Apollo

Apollo ne rend jamais un mobile dans sa réponse : il vérifie les numéros et les POSTe
quelques minutes plus tard à une `webhook_url` qu'il EXIGE. Cette URL est désormais
générée par oto, à chaque reveal (`apollo_receiver.py`) :

    POST <OTO_MCP_PUBLIC_URL>/api/receivers/apollo/phones/{token}

- **Le jeton est l'autorisation.** 256 bits d'aléa, propre à UNE commande, stocké en
  empreinte SHA-256 seulement, masqué au journal comme tout paramètre `{token}`. Inconnu,
  abandonné ou expiré : 404, rien d'écrit. Un jeton qui n'a pas la forme émise (43
  caractères base64url) rend 404 avant que le corps soit lu.
- **La commande naît AVANT l'appel à Apollo** (table `apollo_phone_reveals`, révision
  `0027`) : Apollo peut livrer avant de nous rendre la main. Apollo ne trouve personne ou
  l'appel échoue : la commande est retirée. Sinon son `request_id` la rejoint.
- **Une livraison se garde une fois.** Une retentative répond 200 `duplicate: true` et
  n'écrase rien. Corps borné à 1 Mo, jugé en flux (413) ; illisible, 400. Seule une
  panne de notre côté rend 500 — le seul cas où la retentative d'Apollo est la bonne.
- **Lit qui a accès au connecteur, par la clé qui a payé.** La commande garde la portée
  de la clé Apollo résolue (`niveau:entité:compte`, `apollo_receiver.portee`). Le lecteur
  résout sa clé Apollo AVANT toute lecture ; il ne voit que les commandes payées par
  cette même clé. Sans accès au connecteur, rien n'est lu ; deux comptes sans org, chacun
  sur sa clé, ne se lisent pas.
- **L'id rendu par l'appel fait foi** : il remplace celui d'un POST arrivé avant, qu'Apollo
  ré-écho en nombre (abîmé par le float64).
- **L'index `(cle_portee, request_id)` n'est PAS unique**, délibérément : trouvé au banc, un
  index unique faisait répondre 500 à la seconde commande d'un même identifiant, et
  Apollo réessaie un 500 sans fin. L'unicité d'une livraison est tenue par le jeton.
- **Rétention : trente jours**, celle d'Apollo. La lecture filtre l'échéance ; la purge
  est un travail de maintenance (`oto-mcp maintenance apollo-phones`, dans `all`).

`apollo_reveal_phone_result` résout la clé Apollo, puis lit **ce qu'Apollo nous a livré
d'abord** — sans appel — et ne sonde Apollo (`webhook_result/{id}`) qu'en repli : POST pas encore
arrivé, refusé, ou commande antérieure à ce lot. La réponse garde sa forme (`done`,
`retry_after_seconds`, `result.webhook_result.people[].phone_numbers[]`,
`dnc_status_cd`). Avec `datastore` (+ `row_id`, ou `match_column` pour un lot), les
numéros vont directement dans le tableau — le premier mobile dans `phone_column`, son
type et son `dnc_status_cd` dans la couche `comment` — et seuls des comptes reviennent.

⚠️ **La forme du corps POSTé n'a pas été relevée en vrai.** Apollo documente que son
sondage rend « le même contenu que le POST » : le corps est stocké et servi tel quel.
Une seule forme de repli est reconnue, et nommée (`apollo_receiver.enveloppe`) : un
`people[]` de premier niveau, rangé sous `webhook_result`. Le premier reveal réel dira
laquelle vaut ; si c'est une troisième, le repli Apollo continue de servir.

**`webhook_url` est retiré du schéma, pas du contrat.** Une procédure écrite avant ce
lot le passe encore : il est accepté (`exclude_args` de FastMCP), IGNORÉ, et la réponse
porte `deprecation`. `exclude_args` est déprécié côté FastMCP ; le jour où il disparaît,
`tests/test_apollo_receveur.py` rougit sur l'appel qui le porte.

## 2. Ce qui reste

`lemlist_create_lead` et `hubspot_object` prennent encore la personne en arguments :
leurs variantes par référence (`*_push_rows`, sur `datastore/par_reference.py`, déjà
utilisé ici pour écrire les numéros dans un tableau) forment le lot suivant. Le relevé
du 28/09/2026 (hors de ce dépôt) compte, en plus, seize outils qui livrent à une URL fournie par l'appelant (webhooks lemlist/folk/grain/
granola/linear/tally/webflow/signwell, `lemlist_enrich*`, `fireflies_transcript`…) et
une soixantaine d'écritures qui prennent la personne en arguments (envois d'e-mails et
de messages, CRM, ATS). Le patron est le même : un receveur généré par oto pour les
premiers, un `<ns>_push_rows` ou un destinataire désigné par référence pour les seconds.
