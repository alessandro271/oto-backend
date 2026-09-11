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

## usage — quel SITE d'un grand compte pèse

`foncier_icpe(op="emissions", departement= | code_insee= | siret= | annee=)` — le registre irep : les émissions déclarées **par établissement**, avec siret et coordonnées. c'est le complément de `foncier_beges`, qui porte sur l'organisation entière et ne dit jamais où : ici on nomme le site, donc l'adresse. mesuré sur le 59 — arcelormittal france à 5,995 mt de co2 fossile.

⚠️ **89 % des quantités du registre valent « < seuil »** (56 848 lignes sur 64 045 en 2024) : l'exploitant déclare SOUS le seuil de déclaration. elles sortent en `quantite: null` + `sous_seuil: true`, jamais en zéro, et se rangent APRÈS les quantités connues — elles informent, elles ne classent pas.

⚠️ le co2 existe en trois libellés : fossile (le défaut), biomasse, et le total qui somme les deux. lire le total comme du fossile gonfle un site qui brûle du bois.

## usage — le propriétaire d'un bâtiment

`foncier_proprietaire(code_commune= | siren= | emprise_min= | batiment_groupe_id=)` — la bdnb (cstb) relie un groupe de bâtiments au **siren de son propriétaire**, quand celui-ci est une personne morale. c'est la seule source publique qui va d'un lieu à une entreprise **sans rapprochement d'adresse** : la relation vient du foncier, elle est exacte. elle marche dans les deux sens — `code_commune` + `emprise_min=1000` liste les grands bâtiments d'une commune avec leur propriétaire, `siren=` liste tout ce que possède une société. chaque ligne porte en plus l'emprise au sol, l'usage, l'année de construction, la classe dpe et les **consos pro électricité et gaz du bâtiment** (kwh/an, millésime 2020).

⚠️ **un bâtiment absent n'est pas un bâtiment sans propriétaire.** seules les personnes morales diffusées dans majic y figurent : les personnes physiques — sci en nom propre, exploitants agricoles, artisans — sont anonymisées à la source par la dgfip. un balayage de commune ne rend donc jamais tout le bâti, et `couverture_partielle` le rappelle sur chaque réponse.

⚠️ l'api amont sert **10 lignes par appel** : `limit` au-delà de 10 coûte un aller-retour par tranche, comptés dans `requetes`. `total` est ce qui a été RENDU, jamais ce qui existe — la source ne publie aucun compte.

## usage — remonter d'un site à l'entreprise

les deux étages rendent des adresses ou des iris, jamais un siret. la résolution se fait avec `fr_stock_search` (connecteur `sirene`) sur la commune insee et les **sous-classes naf à 5 caractères** — sirene ne sait pas lire une division à 2 chiffres.

⚠️ le rapprochement se fait sur l'adresse de l'**établissement**, jamais du siège : une usine et son siège social sont couramment à des centaines de kilomètres l'un de l'autre.

⚠️ pas de préfiltre sur la tranche d'effectif : elle est non renseignée pour une part majoritaire du répertoire industriel, y compris chez de vraies cibles multi-sites.

quand la conso est masquée ou le site introuvable, `foncier_icpe(code_insee=…)` donne les installations classées de la commune **avec leur siret** — une autre voie vers les sites industriels lourds. ses `rubriques` disent ce que le site fait vraiment, et `rubriques_energie` isole celles dont l'activité EST une consommation : 2910/3110 combustion, 2920/2921 froid et refroidissement, 4735/1185 réfrigération industrielle. ⚠️ une quantité autorisée est en m³ ou en mw installés, **jamais en kwh** : c'est une magnitude, pas un compteur.
