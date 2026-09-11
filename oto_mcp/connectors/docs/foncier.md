## usage — site, parcelle & immobilier

tout ce qui caractérise un **site** physique en france : géocodage, cadastre, bâti, risques, solaire, prix immobiliers — open data, sans clé.
- `foncier_geocode(adresse)` puis `foncier_site(op="parcelle")(lat, lon)` / `foncier_site(op="bati")(lat, lon)` — coordonnées, parcelle cadastrale, emprise bâtie et CES réel
- `foncier_icpe(siret=… | code_insee=…)` — installations classées (régime, seveso, ied, inspections dreal)
- `foncier_dvf(op="prix_m2")(code_commune)` / `foncier_dvf(op="comparables_adresse")(adresse)` — stats €/m² et ventes comparables dvf
- `foncier_site(op="solaire")(lat, lon, kwc)` — productible photovoltaïque

## usage — consommation électrique et gros consommateurs

`foncier_conso_elec` sert aussi bien la prospection PV qu'un ciblage commercial : « les industriels de tel secteur au-dessus de tant de MWh », par département, commune ou métropole.

**deux étages de réseau, et ils ne sont pas interchangeables.**
- `reseau="distribution"` (défaut) lit enedis : la conso par adresse, avec la division naf.
- `reseau="transport"` lit odré (rte) : les sites raccordés au réseau de transport, **totalement absents d'enedis** — et ce sont les plus gros consommateurs du pays. saint-jean-de-maurienne rend zéro adresse en naf 24 chez enedis et 1 702 616 mwh côté odré.
- `reseau="les_deux"` pour une liste qui ne ment pas. le millésime du transport retarde d'un an : `avertissement_millesime` le dit plutôt que de laisser sommer deux années.

**deux mailles, et l'une fait disparaître des sites.** enedis publie **une ligne par adresse ET par division naf**.
- `maille="ligne"` (défaut, comportement historique) rend les lignes telles que publiées. seuiller ligne à ligne **rate les sites** dont chaque division est sous la barre mais dont le total la dépasse.
- `maille="site"` somme les divisions d'une adresse et applique `min_mwh` **après** la somme. c'est presque toujours ce qu'on veut. chaque site porte alors `naf2_principal`, `naf2_detail` et `multi_naf2`.

**viser un secteur : `naf2`, pas `secteur`.** `secteur` ne connaît que industrie / tertiaire / agriculture — un hôpital et une tour de bureaux y sont la même chose. `naf2` prend les divisions à deux chiffres (`["24","23","86"]`), la maille dans laquelle enedis publie. l'étage transport, lui, ne porte aucun code naf : le secteur n'y arrive qu'après résolution vers siren.

**des mwh par an, jamais des gw.** aucun open data français ne publie la puissance souscrite : un seuil « 2 gw » n'est pas mesurable, un seuil « 2 gwh/an » l'est.

**ce qui n'est pas localisable est compté, pas caché.** enedis publie des lignes sans adresse — de la conso réelle qu'on ne peut rattacher à aucun site. elles ne sortent jamais comme des sites, et remontent dans `lignes_ignorees` / `mwh_ignores`.

un périmètre (`dept`, `code_commune` ou `code_epci`) est obligatoire sur la distribution : avec `maille="site"` le seuil ne peut pas être poussé au serveur. l'étage transport en est dispensé, il tient en ~1 600 lignes nationales.

## usage — ce que le compteur ne dit pas

la consommation décrit un site sans le qualifier, et ne localise pas tout. deux sources prennent le problème par l'autre bout.

- `foncier_dpe(op="tertiaire")(code_commune= | departement=)` — le parc **non résidentiel** : hôpitaux, enseignement, bureaux, commerces, restauration. enedis dit COMBIEN un site consomme, ce jeu dit CE QUE le bâtiment est — secteur erp, surface shon, étiquettes. ses coordonnées sortent **déjà en lambert 93**, donc une ligne se rapproche d'un établissement sans géocodage intermédiaire. `sans_position` compte les diagnostics non géocodés : ils ne sont jamais placés au centre de leur commune.
- `foncier_beges(siren= | naf= | annee= | obligee=)` — les **bilans ges déclarés** (~11 800, dont ~7 000 obligés). ici la clé est le **siren**, pas l'adresse : le bilan se joint directement à l'organisation, y compris pour les sites qu'aucun réseau ne localise. ⚠️ l'année de reporting n'est pas l'année de publication — un bilan publié en 2026 peut porter sur 2015. ⚠️ un poste d'émission absent n'est pas un zéro : les totaux ne somment que le déclaré, et `postes_declares`/`postes_absents` disent sur quoi ils portent. chaque bilan porte en outre trois choses qui ne sont pas des émissions : `contact` — le **responsable du suivi** déclaré, avec sa fonction, son téléphone et son courriel, publiés par l'ademe (absent = masqué à la source par le déclarant, pas introuvable) ; `entites_consolidees` — les siren du périmètre consolidé, soit une table filiale→tête de groupe **déclarée** (ni détention ni mandat social) ; et `electricite` — une consommation en mwh **déduite** du poste 2.1 par le facteur moyen français, marquée `certitude: infere` et rendue avec son facteur. c'est la seule voie publique vers une conso rattachée à une personne morale **nommée** : enedis (adresse) et rte (iris) sont l'un et l'autre anonymes.

## usage — remonter d'un site à l'entreprise

les deux étages rendent des adresses ou des iris, jamais un siret. la résolution se fait avec `fr_stock_search` (connecteur `sirene`) sur la commune insee et les **sous-classes naf à 5 caractères** — sirene ne sait pas lire une division à 2 chiffres.

⚠️ le rapprochement se fait sur l'adresse de l'**établissement**, jamais du siège : une usine et son siège social sont couramment à des centaines de kilomètres l'un de l'autre.

⚠️ pas de préfiltre sur la tranche d'effectif : elle est non renseignée pour une part majoritaire du répertoire industriel, y compris chez de vraies cibles multi-sites.

quand la conso est masquée ou le site introuvable, `foncier_icpe(code_insee=…)` donne les installations classées de la commune **avec leur siret** — une autre voie vers les sites industriels lourds. ses `rubriques` disent ce que le site fait vraiment, et `rubriques_energie` isole celles dont l'activité EST une consommation : 2910/3110 combustion, 2920/2921 froid et refroidissement, 4735/1185 réfrigération industrielle. ⚠️ une quantité autorisée est en m³ ou en mw installés, **jamais en kwh** : c'est une magnitude, pas un compteur.
