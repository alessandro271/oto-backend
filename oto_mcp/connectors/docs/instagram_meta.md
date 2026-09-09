## prerequisite — un compte instagram professionnel, et une invitation de testeur

oto lit les statistiques de **ton** compte instagram : tu l'autorises une fois, chez instagram, et rien ne transite par facebook.

- il faut un **compte professionnel** — business ou créateur (instagram → paramètres → type de compte). un compte personnel ne donne accès à aucune statistique, chez personne
- ⚠️ **tant que notre application n'a pas passé la revue de meta, seuls les comptes INVITÉS comme testeurs peuvent l'autoriser.** ce n'est pas un réglage de ton côté : si ton compte n'a pas été invité, instagram refusera l'autorisation, et il le fera avec le même message que si tu avais cliqué « refuser »
  - demande l'invitation à l'exploitant de cette instance oto (chez otomata : `oto@otomata.tech`), en donnant ton **nom d'utilisateur instagram**
  - puis accepte-la : **instagram → modifier le profil → applications et sites web → invitations de testeur**
  - reviens ensuite ici et clique « connecter »
- l'autorisation vaut **60 jours**. oto la renouvelle tout seul, tous les jours, tant qu'elle est vivante — tu n'as rien à faire. mais elle ne se renouvelle **que tant qu'elle vit** : si le connecteur est retiré, ou l'instance arrêtée plus de deux mois, il faudra la refaire
- pour révoquer : instagram → modifier le profil → applications et sites web → retirer l'accès. la fiche passera alors « à reconnecter »

## setup — l'url de retour à déclarer chez meta

réservé à l'exploitant de l'instance. il faut une **application meta** avec le cas d'usage « instagram api with instagram login », les permissions `instagram_business_basic` et `instagram_business_manage_insights`, et cette url de retour déclarée au byte près :

{{callback:/api/instagram_meta/oauth/callback}}

les deux coordonnées de l'application se posent ensuite au scope plateforme, une fois pour l'instance :

- `oto_admin_connector_setting(op="set", connector="instagram_meta", key="app_id", value="…")`
- `oto_admin_connector_setting(op="set", connector="instagram_meta", key="app_secret", value="…")`

tant qu'elles manquent, le connecteur reste visible et le bouton « connecter » refuse **en nommant la clé absente** : ce n'est alors pas le compte de l'utilisatrice qui est en cause, et il n'y a rien à reposer de son côté.

⚠️ `app_secret` est un **vrai secret** : il signe l'échange du code. il n'est jamais rendu par une api, jamais écrit dans un journal, et `oto_admin_connector_setting` est réservé à l'administration de la plateforme. les préproductions et la production n'ont pas la même url de retour — les deux se déclarent chez meta, sinon un consentement lancé depuis l'une échoue sur un `redirect_uri_mismatch`.

## usage — profil, publications, portée

lecture seule. rien n'est publié, modifié ni supprimé — les permissions demandées ne le permettraient pas.

- `instagram_meta_get_profile` pour commencer : il confirme **quel** compte est connecté (nom d'utilisateur, abonnés, nombre de publications)
- `instagram_meta_get_recent_media` liste les dernières publications avec leurs likes et commentaires — c'est là qu'on récupère l'`id` à passer aux insights
- `instagram_meta_get_media_insights` détaille **une** publication : portée, vues, enregistrements, partages, interactions. les métriques dépendent du type (post de fil, reel, story) et sont choisies pour toi
- `instagram_meta_get_account_insights` donne le compte entier sur les N derniers jours (**30 au maximum** — c'est la fenêtre de l'api, pas un choix d'oto)
- `instagram_meta_get_best_hours` classe les meilleures heures et jours de publication d'après l'engagement moyen des dernières publications

## note — ce que ces chiffres sont, et ne sont pas

- **les meilleures heures sont une heuristique locale**, pas une statistique d'instagram : oto la calcule sur l'engagement moyen des publications qu'il vient de lire. le `sample_size` rendu avec le résultat est là pour ça — un classement sur six publications ne vaut pas un classement sur quarante
- les heures sont celles des horodatages d'instagram (**utc**) : l'api ne dit pas dans quel fuseau publie le compte, et convertir au hasard décalerait le classement sans que rien ne le signale
- **`profile_views` et `website_clicks` n'existent plus** dans cette variante de l'api. la métrique qui les remplace est `profile_links_taps` (les clics sur les liens du profil) — un chiffre plus étroit, pas le même
- ce sont les **statistiques**, pas les messages : les dm ont leur propre connecteur, avec sa propre connexion

## note — ce qu'oto voit, et ce qu'il ne voit pas

- oto ne voit **que ce que ce compte voit** : c'est le compte, et lui seul, qui définit le périmètre
- l'autorisation ne porte **aucune écriture** : même en cas d'erreur, rien ne peut être publié en ton nom
- tu peux la retirer quand tu veux depuis instagram (applications et sites web) : oto le constatera au prochain appel et la fiche te le dira
