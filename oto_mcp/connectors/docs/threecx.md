## prerequisite — tes accès 3cx

le connecteur lit ton standard 3cx avec son adresse et un des deux accès suivants, choisi par `auth_mode`. les droits de cet accès bornent ce qui est visible.
- `base_url` — l'adresse https du client web 3cx, celle de la barre d'adresse quand tu ouvres 3cx dans ton navigateur (ex. `https://votre-societe.3cx.fr`)
- `auth_mode=user` : `username` et `password` — un compte 3cx qui voit les appels et les enregistrements des groupes voulus (un responsable de groupe suffit) ; la double authentification doit y être désactivée ; un compte dédié vaut mieux qu'un compte personnel
- `auth_mode=api_client` : `client_id` et `client_secret` — un client api créé dans la console d'administration 3cx (intégrations > api), si ta licence le permet ; rôle system admin pour voir tout le standard
renseigne `base_url`, `auth_mode` et la paire correspondante dans tes clés de connecteur oto sous `3cx`

## usage — traces d'appels et enregistrements

- `threecx_call(date_from=…, date_to=…)` une page du journal d'appels ; `recorded_only=true` garde les segments enregistrés ; continuer avec `skip=next_skip` tant que `next_skip` n'est pas nul
- `threecx_call(op="export", date_from=…, date_to=…)` toute la période en un fichier csv (séparateur `;`, durées en secondes) : les traces d'appels d'une journée ou d'une semaine, à la place d'un export quotidien
- `threecx_recording(rec_id=…)` l'audio d'un enregistrement, avec le `SrcRecId` ou le `DstRecId` d'une ligne du journal ; rendu en lien signé de courte durée
- le connecteur **n'écrit jamais** dans 3cx
