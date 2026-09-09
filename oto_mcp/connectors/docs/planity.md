## prerequisite — l'email et le mot de passe de ton compte planity pro

planity n'a pas d'api publique : oto rejoue ta connexion à [pro.planity.com](https://pro.planity.com) comme le ferait ton navigateur. renseigne l'**email** et le **mot de passe** de ton compte planity pro — ils sont chiffrés au coffre, jamais rendus en clair, et servent uniquement à ouvrir la session.
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

## note — le code pin administrateur de planity ne protège rien ici

dans planity, le **pin administrateur est vérifié dans l'interface seulement** : le saisir ne déclenche aucune vérification côté serveur. autrement dit, qui détient l'email et le mot de passe du compte a le même accès en lecture que qui connaît le pin — y compris aux écrans que planity garde derrière lui. c'est ce que la pose de ce credential engage de plus lourd, et ça ne dépend pas d'oto : c'est ainsi que planity est fait.

## note — ce que planity ne rend pas

- la ventilation par collaboratrice de `planity_get_revenue_breakdown` (`by_seller`) revient vide côté planity, même en lui passant l'équipe. la bonne réponse est `planity_get_seller_stats`, qui passe par un autre endpoint — un `by_seller` vide n'est donc pas un salon sans ventes
- les statistiques de rendez-vous agrégées de planity ne sont pas exposées : leur appel attend un paramètre que nous n'avons pas résolu
