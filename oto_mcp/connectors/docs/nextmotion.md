## prerequisite — ta clé api nextmotion

connecte-toi à l'[application web Nextmotion](https://app.nextmotion.net), puis **Settings → API Keys** → génère une clé et copie-la : elle n'est plus affichée ensuite. colle-la dans tes clés de connecteur oto sous `nextmotion`.
- la clé agit **au nom de l'utilisateur qui l'a générée**, sur les cliniques dont il est employé ; elle n'expire pas, se révoque par « Reroll » ou suppression au même endroit
- l'accès API est inclus dans l'offre Scale, ou en option payante à partir de l'offre Growth (grille publique [nextmotion.net/tarifs](https://www.nextmotion.net/tarifs)) ; qui peut générer une clé selon le rôle dans la clinique n'est pas documenté
- BYO seulement : pas de clé oto partagée

## usage — agenda, catalogue, ventes, leads, patients, statistiques, stock et agrégats

commence par `nextmotion_clinic()` : chaque autre outil demande un `clinic_id`.
- « qui travaille dans la clinique ? » → `nextmotion_practitioner(op="list", clinic_id=…)`
- « l'agenda du jour » → `nextmotion_appointment(op="list", clinic_id=…, date="AAAA-MM-JJ")`
- « quels créneaux libres ? » → `nextmotion_availability(clinic_id=…, start_date=…, end_date=…)`
- « déplace ce rendez-vous » → prends UN créneau de `nextmotion_availability`, puis `nextmotion_appointment(op="reschedule", appointment_id=…, visit_type_opening_hour_id=<id du créneau>, time_slot=<time_slot du créneau>)`
- « qu'est-ce qu'on propose, à quel prix ? » → `nextmotion_catalog(kind="visit_type"|"treatment_type"|"treatment_pricing"|…, clinic_id=…)`
- « les devis / factures » → `nextmotion_quote(op="list", clinic_id=…)`, `nextmotion_invoice(op="list", clinic_id=…)`
- « les factures de janvier » → `nextmotion_invoice(op="list", clinic_id=…, invoiced_from="2026-01-01", invoiced_to="2026-01-31")`
- « quels lots expirent bientôt, qu'est-ce qui est en rupture ? » → `nextmotion_product(op="list", clinic_id=…, expiring_within_days=30)` ou `stock_state="low"|"out"`
- « quelles salles, quels appareils, quelles plages, qui est absent ? » → `nextmotion_calendar(kind="room"|"device"|"opening_hour"|"absence", clinic_id=…)` (`show_all=True` pour toute la clinique, pas seulement l'utilisateur de la clé)
- « les demandes de rendez-vous en ligne à traiter » → `nextmotion_calendar(kind="appointment_request", clinic_id=…, request_status="new")`
- « où en sont les patients du jour ? » → `nextmotion_journey(clinic_id=…, start_date="AAAA-MM-JJ", end_date="AAAA-MM-JJ")`
- « forfaits, répartitions comptables, produits du catalogue » → `nextmotion_catalog(kind="treatment_package"|"accounting_distribution"|"global_product", clinic_id=…)` ; les soins d'un forfait → `op="items"`, la répartition par praticien d'un tarif ou d'un forfait → `op="distributions"`
- « qui a payé quoi, par quel moyen ? » → `nextmotion_payment(op="list", clinic_id=…)` ou `invoice_id=…` pour une facture
- « le chiffre d'affaires par mois, par type de soin » → `nextmotion_statistics(kind="appointment_income"|"treatment_types"|"treatment_types_income", clinic_id=…, period_type="month")`
- « combien ce patient a-t-il facturé, payé ? » → `nextmotion_patient_stats(patient_id=…)` avec l'id servi par un rendez-vous, un devis ou une facture
- « le pipeline des prospects » → `nextmotion_lead(op="list", clinic_id=…)`, et les libellés de source ou de statut → `nextmotion_setting(kind="object_label", clinic_id=…, label_types=["lead_source"])`
- « d'où viennent nos patients, quel âge, quel genre ? » → `nextmotion_patient_demographics(clinic_id=…, by=["department","age_band"])` — effectifs seulement ; le profil socio-démographique d'une commune (population, revenus) se lit en open data : `urba_socio(code_insee)`
- « nos machines tournent-elles ? » → `nextmotion_device_usage(clinic_id=…, start_date=…, end_date=…, period_type="month")` — 93 jours au plus par appel
- « abonnement Nextmotion, moyens de paiement, gabarits, modèles de questionnaires, webhooks » → `nextmotion_setting(kind="feature"|"payment_medium"|"communication_template"|"document_template"|"survey_form"|"webhook", clinic_id=…)`
- « qui est ce patient ? » → `nextmotion_patient(op="get", patient_id=…)` avec l'id servi par un rendez-vous, un devis, une facture ; « retrouve Mme X » → `nextmotion_patient(op="list", clinic_id=…, search="X")`
- « crée / corrige la fiche d'un patient » → `nextmotion_patient(op="create", clinic_id=…, data={"email": …, "first_name": …, "last_name": …, "gender": …})` ou `op="update", patient_id=…`
- « ajoute une salle, un appareil, une absence, une plage » → `nextmotion_calendar(kind=…, op="create", clinic_id=…, data={…})` ; modifier / supprimer → `op="update"|"delete", item_id=…`
- « déplace ou modifie ce rendez-vous » → `nextmotion_appointment(op="update", appointment_id=…, data={"calendar_event": {"start_time": …, "end_time": …}})`
- « crée un type de soin, un forfait, change un tarif » → `nextmotion_catalog(kind="treatment_type"|"treatment_package"|…, op="create"|"update", …, data={…})` ; lignes d'un forfait → `op="add_item"|"set_items"`, répartition par praticien → `op="set_distributions"`
- « valide ce devis, encaisse cette facture, fais un avoir » → `nextmotion_quote(op="validate", quote_id=…)`, `nextmotion_invoice(op="pay", invoice_id=…, data={"card": "100.00"})`, `nextmotion_invoice(op="credit_note", clinic_id=…, data={"patient": …, "items": [...]})`
- « ajoute ce prospect, convertis-le en patient » → `nextmotion_lead(op="create", clinic_id=…, data={…})`, `nextmotion_lead(op="convert", lead_id=…)`
- « note cet appel, envoie le devis par email » → `nextmotion_communication(kind="call"|"message", clinic_id=…, data={…})`

## note — factures par période : un parcours complet, borné

- l'API Nextmotion ne filtre pas les factures par date et ne documente pas leur ordre : l'outil lit **toutes** les pages (100 factures par appel) et garde celles dont `invoiced_time` tombe dans la période, bornes incluses
- le parcours est plafonné par `max_pages` (20 par défaut, 100 au plus) ; la réponse dit `pages_lues`, `factures_parcourues` et `complet`
- `complet: false` = résultat **partiel** : relance avec `offset=<offset_suivant>` et la même période pour lire la suite
- avec une période, `limit` est refusé et `offset` est le point de départ du parcours
- pas de filtre de période sur les devis : un devis n'a pas de date de facturation, et sa date d'émission peut être vide

## note — stock produits : pas de lien avec les factures

- `nextmotion_product` lit le stock de la clinique (un lot par ligne : numéro de lot, péremption, niveaux de stock, prix unitaire, produit et marque) ; un lot se crée, se corrige (niveaux, péremption) et se supprime
- l'API Nextmotion n'expose **aucun consommable ni lot par facture ou par soin** : une ligne de facture porte l'acte et ses montants, jamais les lots consommés, et rien ne relie un lot à une facture ou à un patient

## note — patientèle et appareils : des agrégats, jamais une ligne

- `nextmotion_patient_demographics` lit toute la liste des patients pour **compter** : par code postal, département, ville, pays, genre ou tranche d'âge ; aucune ligne, aucun id, aucun nom ne sort, et **une case de moins de 10 patients est masquée** (seul le total masqué est rendu) — croiser beaucoup de dimensions masque beaucoup : commence large
- l'âge sort en tranche (0-17, 18-24, 25-34, 35-44, 45-54, 55-64, 65+), jamais en date de naissance ; le département se déduit d'un code postal français, « étranger » si le pays n'est pas la France
- `nextmotion_device_usage` compte, par appareil, les rendez-vous tenus, leurs minutes et les non tenus, en lisant l'agenda jour par jour : c'est l'usage **réservé**, pas l'usage réel de la machine (tirs, durée effective), que Nextmotion ne connaît pas ; aucun taux d'occupation, l'API ne donne pas la capacité d'un appareil

## note — données de santé : ce qui n'est pas servi

- **aucun contenu médical** : antécédents, photos et médias, ordonnances et leur signature, consentements signés, soins réalisés, consultations, visites et leurs notes, réponses aux questionnaires restent hors du connecteur, comme le chat avec les patients ; ni devis ni facture ne se créent ici (Nextmotion ne les crée que sous une consultation)
- **ne se suppriment pas** : un patient, une facture, un paiement (une pièce comptable se corrige par un avoir ou une mise à jour)
- **tout ce qui sort passe par une liste blanche** écrite d'après la spec : un champ que Nextmotion ajouterait demain ne sort pas, et `fields=["*"]` rend la vue par défaut, jamais le brut
- **l'identité du patient sort par `nextmotion_patient` seul** : nom, prénom, email, téléphone, date de naissance, âge, genre, adresse, code postal, ville, pays, consentements de contact, numéro de patient, archivé — **jamais** les commentaires du praticien, la photo ni les coordonnées GPS, et `doctor_comments` est refusé en écriture
- **ailleurs, le patient n'est servi que par son id** (rendez-vous, parcours, devis, factures, paiements, appels, avoirs, aperçus `dry_run` compris) ; quand l'identité est utile, cet id se résout par `nextmotion_patient(op="get", patient_id=…)`. `nextmotion_patient_stats` rend ses totaux financiers et ses dates de visite
- **un lead sert son identité de contact** (nom, prénom, email, téléphone) ; ses notes et sa référence externe s'écrivent mais ne ressortent jamais ; aucune recherche par nom de lead n'est proposée
- **la personne d'une demande en ligne n'est pas relue** : ni nom, ni coordonnées, ni date de naissance ; elle s'écrit (création d'une demande) mais ne ressort pas
- retirés aussi : commentaires du praticien, titres, notes et textes de rappel des évènements d'agenda, titres de devis/facture, détails de ligne, document PDF, lien vers le soin réalisé, numéro, notes et transcription d'un appel, destinataire d'un message, et tout texte libre, **sans option pour obtenir le brut**
- restent en texte les libellés du catalogue (type de visite, nom d'une ligne ou d'un sous-tarif, détail d'un tarif), les étiquettes (source, statut, soin souhaité et zone d'un lead) et le nom des praticiens ; si un praticien a saisi le nom d'un patient dans un libellé de ligne, il passerait

## note — ventes : lignes complètes, statistiques, réglages

- une ligne de devis ou de facture porte son prix, sa quantité, sa remise, sa marge (`markup`), sa TVA et ses sous-tarifs (`subpricing` : nature, prix, TVA, code comptable, part clinique ou praticien)
- un paiement porte le montant par moyen (carte, espèces, chèque, virement, Stripe, avoir, moyens personnalisés) et sa facture, projetée comme dans `nextmotion_invoice`
- les statistiques rendent des graphiques (`title`, `labels`, `datasets`) ; le bloc `meta` de l'API, non décrit, n'est pas servi
- gabarits de communication et de documents, modèles de questionnaires : métadonnées et champs de fusion à la lecture (type, nom, activé), corps complet à l'écriture ; webhooks : leurs en-têtes, qui portent d'ordinaire un secret, s'écrivent mais ne sont jamais rendus, aperçu compris

## note — écritures : un aperçu d'abord

- **toute écriture a `dry_run=True` par défaut** (création, modification, suppression, report, validation, encaissement, avoir, conversion, envoi) : l'appel valide `data`, relit l'objet visé et rend ce qui partirait, sans rien écrire. passe `dry_run=False` pour agir
- le corps passe en `data`, contrôlé contre les champs que la spec Nextmotion accepte pour CET op : **un champ inconnu est refusé, nommément**, jusque dans les objets imbriqués — jamais ignoré ; un champ requis manquant aussi
- une liste « complète » remplace : `sub_visit_types` d'un type de visite, `pricings` d'un type de soin, `op="set_items"` d'un forfait, `op="set_distributions"` — un élément omis est supprimé
- une valeur `null` n'est pas envoyée : on ne vide pas un champ par cet outil
- `nextmotion_communication(kind="message")` **envoie** un email, SMS ou WhatsApp au patient, pour un devis, une facture ou un document administratif seulement — jamais une ordonnance, un consentement ou un document médical
- **jamais de notification implicite** : la modification d'un rendez-vous préviendrait le patient par défaut (`send_appointment_modified_email|sms` valent `true` dans la spec) ; l'outil les envoie à `false` sauf si tu les passes à `true`, et l'aperçu dit qui serait prévenu (`notifie_le_patient`)
- que Nextmotion prévienne le patient lors d'un report ou d'une suppression n'est pas documenté
- les lignes d'un devis ou d'une facture ne s'éditent pas ici : chacune exige l'id d'un soin clinique ; une ligne d'avoir ne référence ni soin ni forfait extrait
- `pay` et `credit_note` valident la pièce par défaut côté Nextmotion (`do_validate: true`) ; passe `do_validate: false` pour garder un brouillon
- dérivé de la spec OpenAPI publique, jamais exercé avec une vraie clé
