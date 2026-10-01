## prerequisite — API ID + API Token aircall

un admin aircall crée une clé dans le Dashboard Aircall (Company Settings → API Keys → *Add a new API key*), puis colle les **deux** valeurs dans oto : l'`API ID` et l'`API Token`. Référence : [authentification de l'API Aircall](https://developer.aircall.io/api-references/#basic-auth-aircall-customers).
- ⚠️ **le token n'est montré qu'UNE fois**, à la création : Aircall ne le conserve pas en clair. S'il est perdu, il faut créer une nouvelle clé
- une clé donne accès à **toute la société** : Aircall n'offre pas de portée plus fine
- byo-only : pas de clé oto partagée — ce sont les appels de ta société, chaque organisation pose la sienne
- la transcription, le résumé, les sujets, le sentiment et les actions à mener ne sont servis qu'aux sociétés abonnées à l'**offre IA d'Aircall** (AI Assist ou AI Assist Pro)

## usage — ce qui s'est passé au téléphone

- « les appels d'hier » → `aircall_calls(date_from="…", date_to="…", order="desc")`
- « les appels avec ce client » → `aircall_calls(op="search", phone_number="+33…")` ; « les appels de tel agent » → `op="search", user_id=…`, l'id venant de `aircall_users`
- « l'enregistrement de cet appel » → `aircall_calls(op="get", call_id=…)` : `recording` (ou `voicemail`) est un lien mp3
- « de quoi a-t-on parlé ? » → `aircall_call_ai(call_id=…, op="summary")`, puis `op="transcription"` pour le mot à mot, `op="action_items"` pour les suites à donner
- « qui est ce numéro ? » → `aircall_contacts(op="search", phone_number="+33…")`
- le connecteur **n'écrit jamais** dans Aircall

## note — ce qui trompe

- ⚠️ **les liens d'enregistrement et de messagerie vocale expirent** : 1 heure pour `recording`/`voicemail`, 3 heures pour leurs versions courtes (`short_urls=true`). Les redemander juste avant usage, ne jamais les stocker
- ⚠️ **six mois d'historique d'appels seulement**, et au plus 10 000 appels ou contacts par pagination : resserrer avec `date_from` pour aller plus loin
- `duration` inclut la sonnerie : la durée de conversation est `ended_at - answered_at`. Les dates de l'API sont en secondes UNIX, UTC
- les contacts synchronisés depuis une intégration tierce (CRM…) ne sont **pas** servis par l'API, même s'ils s'affichent dans Aircall
- une transcription ou un résumé absent (404) : l'appel n'a pas été analysé, ou la société n'a pas l'offre IA

## note — les limites d'usage

120 requêtes par minute pour toute la société. Le connecteur attend la remise à zéro du compteur quand elle est proche, une seule fois ; sinon il le dit, et il suffit de réessayer une minute plus tard.
