## prerequisite — l'email et le mot de passe de ton compte planity pro

oto se connecte à [pro.planity.com](https://pro.planity.com) avec tes identifiants, comme tu le ferais toi-même. renseigne l'**email** et le **mot de passe** de ton compte planity pro — ils sont chiffrés au coffre, jamais rendus en clair, et servent uniquement à ouvrir la session.
- il faut un compte planity **pro** actif, rattaché à au moins un salon (agenda + caisse)
- le compte est personnel : chacun pose le sien, et ne voit que les salons que planity lui ouvre
- **un compte de gestion ouvre PLUSIEURS salons, un compte de salon un seul** : c'est le compte que tu poses qui décide, et `planity_list_salons` te dit lesquels — si tu en attendais quatre et n'en vois qu'un, c'est le compte, pas l'outil
- « tester la connexion » ouvre la session et liste tes salons — si aucun salon ne remonte, le compte s'authentifie mais n'est rattaché à rien

## usage — lire ton agenda, tes clientes et tes chiffres

lecture seule. aucun rendez-vous n'est créé, modifié ni annulé.
- `planity_list_salons` pour commencer : l'`id` rendu est le `salon_id` de tous les autres outils
- agenda — « quels rendez-vous j'ai cette semaine ? » (`planity_list_appointments`, presets `today` / `this_week` / `30d`…), le détail d'un rendez-vous (`planity_get_appointment`), les rendez-vous récurrents (`planity_list_recurring_appointments`, qui n'apparaissent dans AUCUNE liste par date)
- clientes — recherche par nom, téléphone ou email (`planity_search_customers`), fiche (`planity_get_customer`), statistiques et tickets d'une cliente (`planity_get_customer_stats`, `planity_get_customer_receipts`)
- référentiel — l'équipe (`planity_list_employees`), le catalogue de prestations et de produits (`planity_list_services`, `planity_list_products`)
- chiffres — « quel est mon CA du mois ? » (`planity_get_revenue_summary`), le jour par jour (`planity_get_daily_revenue`), la décomposition prestations/produits (`planity_get_revenue_breakdown`), par collaboratrice (`planity_get_seller_stats`), par moyen de paiement (`planity_get_revenue_by_payment_method`), par taux de TVA (`planity_get_revenue_by_vat`), par prestation (`planity_get_service_stats`), le taux d'occupation (`planity_get_occupancy_rate`) et les avis (`planity_get_reviews_stats`)
- caisse, au ticket près — les sessions de caisse (`planity_list_pos_periods`), une session et ses tickets (`planity_get_pos_period`), un ticket en détail (`planity_get_receipt`), la table des moyens de paiement (`planity_list_payment_methods`)
- stock — ce qui bouge (`planity_list_stock_movements`), les fournisseurs (`planity_list_suppliers`), les commandes de réassort (`planity_list_product_orders`), les sorties groupées (`planity_list_mass_stock_removals`)
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

## note — ce que les outils ne rendent PAS des clientes

le connecteur rend le brut et laisse l'agent composer, **sauf sur les données personnelles d'une tierce**. un rendez-vous et un ticket portent, chez planity, le nom, le téléphone, l'email et l'adresse de la cliente ; un ticket y ajoute le commentaire écrit sur elle.
- les outils d'agenda et de caisse rendent une **liste de champs choisis**, et pour la cliente un **identifiant seulement** — pas de nom, pas de contact, pas d'adresse
- `planity_list_appointments` ne rend pas non plus le commentaire libre du rendez-vous (il contient couramment des noms) ; `planity_get_appointment`, appelé pour UN rendez-vous, le rend
- pour la personne derrière un identifiant : `planity_get_customer`. c'est son objet, tu l'as demandé, et rien ne sort tant que tu ne le demandes pas
- ce n'est pas un oubli : la cliente n'est pas dans la conversation, elle n'a rien demandé, et son adresse n'a pas à traverser un échange pour répondre « combien j'ai fait hier »

## note — prévision de commande, en trois appels

il n'y a **pas d'outil de prévision** : la règle (couverture visée, délai fournisseur, familles à réassortir) t'appartient. les outils rendent les faits.
1. `planity_get_revenue_breakdown` sur 90 jours → les quantités vendues par produit, en un appel (`by_product`, `bucket_id` = l'id du produit au catalogue)
2. `planity_list_products` → le stock de chaque produit et ses **lots d'achat** (avec leur prix d'achat, donc la marge)
3. le calcul est à toi : `couverture = stock / (ventes ÷ 90)`, à comparer à ton délai de réassort. `planity_list_stock_movements(product_ids=[…])` donne le détail des mouvements sur les produits qui sortent du lot
- ⚠️ `planity_list_stock_movements` **exige `product_ids`** : les mouvements se lisent un produit à la fois, et il ne balaie pas un catalogue entier tout seul. sans les ids, il refuse tout de suite, sans rien lire
- ⚠️ `stock_threshold` et `stock_ceiling` valent `null` quand le salon ne s'en sert pas — **`null` n'est pas `0`** : une règle qui lirait zéro commanderait tout, tout le temps
- ⚠️ une baisse de stock sans vente n'est pas une anomalie : regarde `planity_list_mass_stock_removals` (inventaire, casse, péremption)

## note — supprimé n'est pas absent

planity conserve ce qu'on supprime : une collaboratrice partie garde son agenda et ses rendez-vous passés, une prestation retirée reste sur les anciens tickets.
- `planity_list_employees`, `planity_list_services` et `planity_list_products` **écartent les supprimés par défaut** — un salon de trois personnes n'en annonce pas sept
- `include_deleted=true` pour l'historique : retrouver la prestation d'un ancien ticket, ou le chiffre d'une collaboratrice partie
- tous les agendas sont lus pour un historique de rendez-vous, y compris ceux des collaboratrices supprimées : les écarter ferait disparaître leur chiffre sans rien qui le signale
- un enfant d'agenda n'est pas toujours une personne (cabine, poste, ressource) : `type` et `title` sont rendus tels que planity les stocke

## note — lire une période avant d'en projeter quoi que ce soit

`planity_get_revenue_summary` et `planity_get_daily_revenue` rendent un bloc `period` : bornes, nombre de jours, fuseau (europe/paris), et surtout `ends_today` / `complete`.
- la plupart des presets s'arrêtent à **maintenant**, pas à la fin de la journée : le dernier jour est partiel
- un rythme journalier calculé sur une telle fenêtre est donc trop bas, et une projection bâtie dessus (« au rythme actuel, il reste N jours ») sort fausse sans que rien ne le signale
- les jours sans encaissement sont **absents** de la série, pas présents à zéro : `days_with_revenue` n'est pas `period.days`
