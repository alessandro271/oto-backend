## usage — urbanisme & territoire

l'enveloppe réglementaire et territoriale d'un point ou d'une commune — open data, sans clé. géocode l'adresse d'abord (`foncier_geocode`).
- `urba_zonage(lat, lon)` — zonage plu/plui opposable (géoportail de l'urbanisme), avec le règlement pdf si dispo
- `urba_risques(code_insee)` / `urba_argiles(lat, lon)` — risques naturels/technologiques et aléa retrait-gonflement des argiles
- `urba_qpv(code_insee)` / `urba_qpv_proximite(lat, lon)` — quartiers prioritaires de la ville
- `urba_epfif(code_insee)` / `urba_socio(code_insee)` — secteurs epfif (île-de-france) et profil socio-démo insee

## usage — qui décide, qui répond, sur une cible publique

sur une commune ou un établissement public, le décideur est un **élu** et l'interlocuteur un **service** : ni l'un ni l'autre n'est un dirigeant sirene, et l'enrichissement payant ne les trouve pas.

- `urba_elus(fonction="maire", code_commune=…)` — le maire, avec la date de sa prise de fonction (utile pour savoir si l'interlocuteur a changé depuis la dernière campagne). `fonction="president_epci"` + `siren_epci=` pour une intercommunalité — seul le président est rendu, pas les milliers de conseillers communautaires. la date de naissance et le sexe, présents dans le fichier source, ne sont **pas** rendus. rapprocher sur le code insee, jamais sur le nom : « sainte-marie » existe des dizaines de fois.
- `urba_annuaire(code_commune= | siren= | type_service=)` — les services publics (~36 000 mairies, préfectures, ddfip…) avec standard, courriel et `responsables` : le **responsable nommé**, sa fonction, souvent son courriel direct. un champ que la source a mal sérialisé sort dans `champs_illisibles`, pas en vide silencieux. ⚠️ servi par opendatasoft : l'egress depuis la box de prod reste à vérifier.

