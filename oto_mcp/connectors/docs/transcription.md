## prerequisite — une clé Mistral, entraînement désactivé

crée une clé API sur la [console Mistral](https://console.mistral.ai/api-keys), puis colle-la dans oto au niveau de l'organisation.
- **avant le premier enregistrement d'un client**, désactive l'usage de tes données pour l'entraînement dans les réglages du compte Mistral : ce sont des voix de particuliers, souvent chez eux
- par défaut chaque organisation paie sa propre minute d'audio chez Mistral (hébergement dans l'UE) ; une org sans clé propre peut se voir accorder une instance plateforme (clé, langue et vocabulaire de cette instance), et sa propre instance passe toujours avant

## setup — langue et vocabulaire

deux réglages facultatifs sur l'instance, non secrets :
- **langue** : le code de la langue parlée (`fr` si vide). `auto` laisse Mistral la détecter
- **vocabulaire** : les mots du métier à bien orthographier (ouvrages, matériaux, noms propres), séparés par des virgules ou des retours à la ligne. Mistral ne prend que des mots isolés : une expression est découpée en ses mots, et les mots de moins de 3 lettres sont écartés (« pompe à chaleur » → `pompe`, `chaleur`). 100 mots au plus ; ce qui est écarté est signalé dans la réponse

une instance = une clé × une langue × un vocabulaire ; un projet se rattache à l'instance voulue par un slot.

## usage — un enregistrement devient une page du projet (asynchrone)

- « transcris la visite déposée sur le projet » → `transcription_create(source={"kind":"project_file","project_id":…,"file_id":…}, _project=…)` — les ids viennent de `oto_project_files op=list`
- `vocabulary` (facultatif) complète, pour cet enregistrement, le vocabulaire de l'instance (noms propres, matériaux du chantier) ; `vocabulary_replace=true` l'utilise seul. 100 mots au plus au total, l'instance passe avant
- le fichier est lu côté serveur, jamais transporté par la conversation (100 Mo au plus, jusqu'à 3 h d'audio ; un fichier de projet reste limité à 25 Mo au dépôt)
- **l'appel ne bloque pas** : il rend tout de suite `{job_id, status:"pending"}` — un enregistrement de 30 min prend de 20 s à 5 min à transcrire, en tâche de fond
- relire `transcription_status(job_id)` jusqu'à `status:"done"` (ou `"failed"` avec `error`) : c'est là que revient la page « Transcription — <fichier> — <date> » (id, titre, lien), un paragraphe par tour de parole avec locuteur et instant en tête (« Locuteur 1 [03:12] »…), le nombre de mots, la durée et les locuteurs — jamais le texte, qui se lit ensuite comme toute page du projet

## usage — depuis un programme (API REST, jeton `oto_…`)

- l'audio lui-même : `curl -H "Authorization: Bearer $OTO_TOKEN" -F file=@reunion.ogg -F vocabulary="Voxtral, Otomata" https://mcp.oto.cx/api/me/projects/<id>/transcriptions/upload` → `202 {job_id, status:"pending"}`
- un fichier que oto sait déjà atteindre : `POST /api/me/projects/<id>/transcriptions` avec `{"source":{"kind":"project_file","file_id":…}}` (ou `url`, `drive`, `gmail`)
- relire `GET /api/me/transcriptions/<job_id>` : sur `done`, en plus de la page, `transcript` rend les tours de parole en données — `[{speaker, start, end, text}]`, en secondes

## note — ce que le connecteur corrige, et ce qu'il ne fait pas

- les segments répétés à l'identique sont fusionnés ; les locuteurs « parasites » que la diarisation invente pour une phrase sont rattachés au locuteur voisin (on garde au plus 3 locuteurs, chacun pesant au moins 10 % de l'enregistrement)
- rien ne se transcrit sans appel : on ne paie que ce qui est demandé
- la transcription ne tire aucune conclusion du texte : c'est la procédure du projet qui dit quoi en faire (fiche de visite, compte rendu…)
