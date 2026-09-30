## prerequisite — tes accès api inqom

l'api inqom s'ouvre avec deux paires : les clés d'application et un compte inqom au nom duquel oto agit. ce compte borne ce qui est visible.
- `client_id` et `client_secret` — les clés d'application api, fournies par inqom (demande-les à ton contact inqom)
- `username` et `password` — le compte inqom utilisé ; inqom recommande un compte système non nominatif du cabinet, qui voit tous ses dossiers
renseigne ces quatre champs dans tes clés de connecteur oto sous `inqom`

## usage — lire la compta d'un dossier

- `inqom_company` les cabinets/pme accessibles, puis `inqom_dossier(op="list", company_id=…)` leurs dossiers, `inqom_dossier(op="get")` la fiche d'un dossier
- `inqom_ref(kind="accounts")` le plan comptable (les tiers : `number_prefix="401"` ou `"411"`), `kind="journals"` les journaux, `kind="periods"` les exercices
- `inqom_balance` la balance sur une période (le détail par tiers est coupé par défaut, `fields=["*"]` le rend)
- `inqom_entry_line(op="count")` puis `op="list"` page par page (1 000 lignes max, `page_number` commence à 1)
- plusieurs comptes d'un coup : `account_prefixes=["6", "7"]` (un compte de résultat), `["401"]` (les fournisseurs) — lignes triées par date, chacune avec `third_party_accounts`, les comptes de tiers (40x/41x) de son écriture : c'est là qu'est le fournisseur d'une charge. lit toute la période : un mois ou un trimestre
- `inqom_document` l'url de téléchargement d'une pièce rattachée à une ligne
- le connecteur **n'écrit jamais** dans inqom : `inqom_entry_create` rend le refus nommé `inqom_write_not_wired`, qui décrit les écritures qu'il aurait créées — rien n'est envoyé ; la saisie se fait dans inqom même
