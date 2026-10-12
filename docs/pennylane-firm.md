# Pennylane (cabinet) — connecteur `pennylane_firm` et relais d'upload

L'API « Firm » de Pennylane, côté **cabinet comptable** : un jeton de cabinet atteint toutes les sociétés du
portefeuille. Transport dans la lib (`oto.tools.pennylane_firm`, client + description déclarative
`connectors/pennylane_firm`) ; ici l'**adaptateur** — cinq outils, un relais d'upload et le manifeste d'un lot.

## Trois connecteurs Pennylane, trois espaces

| connecteur | porte | crédential | `company_id` |
| --- | --- | --- | --- |
| `pennylane` | API Company v2 | clé d'UNE société | ses propres ids |
| `pennylane_firm` | API Firm v1 | jeton de cabinet (palier org) | ids de l'application |
| `pennylaneged` | API interne de la SPA, session navigateur | contexte Browserbase | ids de l'application |

`pennylane_firm` et `pennylaneged` partagent les ids ; `pennylane` non. L'API Firm n'expose ni la forme
juridique, ni la catégorie IS/IR, ni les régimes fiscal et de TVA : ces réglages ne se lisent que par
`pennylaneged_company`.

## Outils

- `pennylane_firm_companies` — une page du portefeuille (filtre `client_code`), ou la fiche d'une société ;
- `pennylane_firm_tree` — dossiers ou fichiers de la GED d'une société, au curseur ;
- `pennylane_firm_create_folder` — écriture ;
- `pennylane_firm_upload_url` — frappe un lien de relais, gardé par le contrôle de doublon ;
- `pennylane_firm_upload_urls` — frappe jusqu'à 100 liens d'un coup, servis par un manifeste (§ « Par lots »).

⚠️ **L'API n'a ni suppression, ni déplacement, ni renommage** : un mauvais dépôt ne se corrige pas par elle
(seulement dans l'interface, ou par `pennylaneged_delete`). D'où le refus du doublon, joué AVANT d'écrire.

Aucune liste blanche de sociétés : le jeton de cabinet ouvre l'écriture sur tout le portefeuille, et
l'org qui le pose en répond.

Débit : 5 requêtes/s par jeton (limiteur de la lib, au niveau du module, partagé par les outils et le relais) ;
un 429 de Pennylane impose une minute d'attente et se rend en refus réessayable.

## Le relais d'upload

Le fichier **transite par le serveur** (choix produit). L'agent appelle `pennylane_firm_upload_url(company_id,
parent_folder_id, name)` ; le poste envoie `curl -F 'file=@<chemin>' '<url>'`, sans jeton ; le serveur dépose
le fichier avec le jeton de cabinet du coffre et rend l'objet fichier créé par Pennylane.

- **Route** `POST /api/relay/{token}` (`api/pennylane_firm.py`), SŒUR de `/api/upload/{token}` et non la même :
  celle-ci lit le corps en mémoire. Nature `NATURE`, aucun JWT, `ContratDeRoute` publié.
- **Jeton** : celui des uploads signés (`upload_tokens.sign/verify`, HMAC, 15 min, `jti` à usage unique) sous
  `typ="relay"` — un jeton d'upload oto ne s'ouvre pas ici, et inversement. Cible scellée : `company_id`,
  `parent_folder_id` (obligatoire : pas de dépôt à la racine), `name`, plus `sub` et `org`.
- **Ordre des contrôles**, du moins cher au plus cher, rien d'irréversible avant le dernier : signature, `typ`,
  expiration → garde d'identité du compte → **org scellée rejouée** (appartenance revérifiée, suspension,
  activation, jeton résolu sous CETTE org — sans quoi la cascade retomberait sur l'org « maison » du compte)
  → doublon → `Content-Length` présent (411) et sous le plafond (413) → place libre (503,
  lien non consommé) → consommation du `jti` (409) → lecture du multipart → relais.
- **Transit disque assumé** : le parseur de Starlette déborde sur disque au-delà d'un mégaoctet. Les octets sont
  comptés pendant la lecture et coupés au plafond ; le temporaire est fermé en `finally`, succès ou échec ; le
  fichier n'est jamais lu en entier, la lib l'envoie à Pennylane par morceaux.
- **Plafond** `OTO_PENNYLANE_FIRM_UPLOAD_MAX_BYTES` (100 Mo) — **provisoire**, la documentation de Pennylane ne
  donne pas sa limite ; à relever après mesure. Distinct de `OTO_MCP_UPLOAD_MAX_BYTES`.
- **Concurrence** `OTO_PENNYLANE_FIRM_RELAY_CONCURRENCY` (4 par processus), sans attente : au-delà, 503
  `relay_busy` + `Retry-After`, le même lien resservant.
- **Aucune connexion base pendant le transfert** : tout ce qui touche base et coffre est lu avant.
- **Journal** : une ligne `tool_calls` par dépôt tenté (`tool="pennylane_firm_relay"`, compte, org, cible,
  octets, id du fichier créé, durée), jamais un jeton.

## Par lots : `pennylane_firm_upload_urls` et le manifeste

Mesure du 12/10 : un agent qui frappe UN lien par appel d'outil passe ~6,7 s par fichier en frappe, contre
0,87 s de transfert. D'où la frappe par lots. Et l'outil ne rend PAS les liens : à ~550 caractères de jeton
chacun, cent liens feraient ~55 Ko que l'agent devrait recopier dans son script — lent, coûteux, fragile. Ils
vont dans un **manifeste** téléchargeable, que le script fourni consomme.

- **Appel** : `pennylane_firm_upload_urls(company_id, items=[{parent_folder_id, name}, …])`, **1 à 100**
  éléments, sinon `empty_batch` / `batch_too_large`. Org de l'appel requise (`no_organization`).
- **Pourquoi 100** : les liens vivent 15 min ; 100 fichiers de 100 Mo (le plafond du relais) sur 4 flux à
  ~9 Mo/s montent en ~280 s, dans le TTL avec de la marge. Sur une montée plus lente, la description conseille
  des lots plus petits pour les gros fichiers. Et la réponse comme le manifeste restent de taille bornée.
- **Contrôles par élément**, dans cet ordre, chacun refusant l'élément SANS faire échouer le lot (liste
  `refused`, avec `code` et `message`) :
  1. nom invalide — 1 à 255 caractères, et ni tabulation ni saut de ligne (ils casseraient le TSV : refusés
     plutôt qu'échappés) → `invalid_name` ;
  2. même dossier et même nom (après `strip`) plus haut dans le lot : le premier gagne → `duplicate_in_batch` ;
  3. dossier introuvable : UNE lecture `list_dms_folders(filter=[{field: id, operator: eq, value: "<id>"}])`
     par dossier DISTINCT (forme `eq` + valeur chaîne, celle mesurée en réel ; l'id rendu est relu) →
     `folder_not_found` ;
  4. nom déjà présent : le cache anti-doublon, une lecture complète par dossier distinct →
     `name_already_exists` ; dossier trop grand pour être lu en entier → `duplicate_check_incomplete` pour
     tous ses éléments.

  Une erreur de Pennylane qui n'est PAS propre à un élément (429, 401, scope, 5xx, société introuvable) fait
  échouer tout l'appel par son refus nommé, et **aucun lien n'est frappé** : toutes les lectures précèdent la
  première signature.
- **Les liens** : les mêmes qu'à l'unité (`relais.frapper`, `typ="relay"`, usage unique, 15 min, mêmes
  contrôles au relais) — rien ne change côté `POST /api/relay/{token}`.
- **Réponse** : `{manifest_url, expires_at, max_bytes, ready: [{parent_folder_id, name}], refused:
  [{parent_folder_id, name, code, message}], script, hint}` — aucune URL de lien. Lot entièrement refusé :
  `manifest_url` à `null`, aucun manifeste.
- **Le manifeste** : les lignes `(parent_folder_id, name, url)` des éléments acceptés, gardées **en mémoire du
  processus** (le serveur est mono-processus) sous un id aléatoire, avec le compte, l'org et l'expiration des
  liens. Magasin borné (`MANIFESTES_MAX` = 500 vivants) et thread-safe : purge des expirés à chaque écriture ;
  plein, refus `manifest_store_full` (503, réessayable) plutôt qu'une éviction silencieuse. ⚠️ **Un redémarrage
  perd les manifestes** : assumé, la route le dit (`manifest_expired`, « mint the batch again ») ; les liens
  déjà téléchargés restent valables.
- **Route** `GET /api/relay/batch/{token}` (`api/pennylane_firm.py`), nature `NATURE`, `ContratDeRoute`
  publié, `{token}` masqué au journal. Jeton `typ="relay_batch"`, cible `{kind: pennylane_firm_manifest,
  manifest_id}`, 15 min : il ne s'ouvre ni sur `/api/upload/{token}` ni sur `POST /api/relay/{token}`, et
  inversement. Contrôles : jeton → 401 `invalid_or_expired_token` ; garde d'identité → 403 ; appartenance du
  compte à l'org scellée → 403 `not_an_org_member` ; manifeste absent ou expiré → 404 `manifest_expired`.
  Sinon 200 `text/tab-separated-values; charset=utf-8` : en-tête `parent_folder_id\tname\turl`, une ligne
  par lien. Relisible tant qu'il vit ; chaque lien reste à usage unique. Base lue hors boucle.
- **Le script** (`SCRIPT_LOT`, bash + coreutils + awk + curl + xargs, constant) :
  `bash <fichier> '<manifest_url>' paths.tsv [results.tsv]`. `paths.tsv` : une ligne par fichier,
  `parent_folder_id<TAB>name<TAB>chemin local`. Il télécharge le manifeste, joint sur (dossier, nom), envoie
  chaque fichier par `curl -sS -F "file=@\"<chemin>\"" <url>` sur **4 flux au plus** (`xargs -0 -P 4`,
  séparateurs NUL : espaces, accents, `;`, `,` passent), réessaie le même lien sur un 503 `relay_busy`, et
  écrit `results.tsv` (`code_http<TAB>name<TAB>chemin`). Une ligne sans lien (refusée, ou déjà servie) y sort
  en `NO_LINK`, sans envoi ; `000` = curl n'a rien envoyé (fichier illisible, réseau).
- **Marche à suivre servie à l'agent** : frapper (≤ 100) → écrire `paths.tsv` → lancer le script UNE fois →
  relire le dossier (`pennylane_firm_tree`). 409 au relais = déjà présent ; 429 = attendre 60 s puis refrapper
  SEULEMENT les échecs. L'API ne supprime ni ne renomme : un dépôt est définitif.

## Le cache anti-doublon

Les NOMS d'un dossier, par (org, société, dossier), 15 min : rempli une fois par une lecture complète du dossier,
consulté à la frappe ET au relais, enrichi par chaque dépôt réussi ; un nom « en vol » est réservé (deux relais
du même nom ne partent pas tous les deux). Un 422 invalide l'entrée. Un dossier trop grand pour être lu en
entier ne conclut jamais « absent » : refus `duplicate_check_incomplete`.

⚠️ **Limite assumée** : un fichier ajouté dans l'interface de Pennylane pendant la vie d'une entrée n'est pas vu ;
le doublon passerait. Le cache est par processus.
