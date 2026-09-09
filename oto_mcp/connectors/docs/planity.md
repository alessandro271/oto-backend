## prerequisite — l'email et le mot de passe de ton compte planity pro

oto se connecte à [pro.planity.com](https://pro.planity.com) avec tes identifiants, comme tu le ferais toi-même. renseigne l'**email** et le **mot de passe** de ton compte planity pro — ils sont chiffrés au coffre, jamais rendus en clair, et servent uniquement à ouvrir la session.
- il faut un compte planity **pro** actif, rattaché à au moins un salon (agenda + caisse)
- le compte est personnel : chacun pose le sien, et ne voit que les salons que planity lui ouvre
- « tester la connexion » ouvre la session et liste tes salons — si aucun salon ne remonte, le compte s'authentifie mais n'est rattaché à rien

## usage — lire ton agenda, tes clientes et tes chiffres

lecture seule. aucun rendez-vous n'est créé, modifié ni annulé.
- `planity_list_salons` pour commencer : l'`id` rendu est le `salon_id` de tous les autres outils
- agenda — « quels rendez-vous j'ai cette semaine ? » (`planity_list_appointments`, presets `today` / `this_week` / `30d`…), le détail d'un rendez-vous (`planity_get_appointment`)
- clientes — recherche par nom, téléphone ou email (`planity_search_customers`), fiche (`planity_get_customer`), statistiques et tickets d'une cliente (`planity_get_customer_stats`, `planity_get_customer_receipts`)
- référentiel — l'équipe (`planity_list_employees`), le catalogue de prestations et de produits (`planity_list_services`, `planity_list_products`)
- chiffres — « quel est mon CA du mois ? » (`planity_get_revenue_summary`), le jour par jour (`planity_get_daily_revenue`), la décomposition prestations/produits (`planity_get_revenue_breakdown`), par collaboratrice (`planity_get_seller_stats`), le taux d'occupation (`planity_get_occupancy_rate`) et les avis (`planity_get_reviews_stats`)
- clientèle — meilleures clientes, nouvelles clientes, fréquence de visite (`planity_get_best_customers`, `planity_get_new_customers`, `planity_get_customer_frequencies`)

## note — ce avec quoi le connecteur s'authentifie

le connecteur s'authentifie avec l'**email et le mot de passe** de ton compte planity pro, et rien d'autre : il n'utilise pas le code administrateur de l'application planity.
- ce que le connecteur peut lire est donc ce que ce compte peut lire — c'est le compte, et lui seul, qui définit le périmètre
- pour restreindre ce qu'oto voit, utilise un compte planity au périmètre plus étroit
- tout est en lecture : aucun rendez-vous n'est créé, modifié ni annulé

## note — ce connecteur demande une configuration de l'instance

le connecteur a besoin, en plus de tes identifiants, de trois **coordonnées de l'application planity** posées une fois par l'exploitant de l'instance oto : `firebase_api_key`, `firebase_app_id`, `rest_api` (réglages de connecteur, scope plateforme).
- si elles manquent, les outils `planity_*` restent visibles mais refusent en le disant, en nommant la clé absente et la commande qui la pose — ce n'est alors pas ton credential qui est en cause, et il n'y a rien à reposer de ton côté
- **ce ne sont pas des secrets** : elles sont publiques par conception (tout navigateur qui ouvre `pro.planity.com` les reçoit), elles appartiennent à planity, et elles n'autorisent rien à elles seules — ce qui autorise, c'est ton mot de passe, qui vit au coffre chiffré. elles peuvent apparaître dans un message d'erreur ou un journal de débogage sans que ce soit une fuite
- si elles ne sont pas dans le code, c'est parce que le client est publié en open source : un connecteur y décrit un protocole, il n'embarque pas les coordonnées d'une entreprise tierce comme s'il était son intégration officielle

## note — ce que planity ne rend pas

- la ventilation par collaboratrice de `planity_get_revenue_breakdown` (`by_seller`) revient vide côté planity, même en lui passant l'équipe. la bonne réponse est `planity_get_seller_stats`, qui passe par un autre endpoint — un `by_seller` vide n'est donc pas un salon sans ventes
- les statistiques de rendez-vous agrégées de planity ne sont pas exposées : leur appel attend un paramètre que nous n'avons pas résolu
