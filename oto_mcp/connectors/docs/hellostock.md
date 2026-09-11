## prerequisite — ton jeton d'API HelloStock, d'un compte administrateur

crée un jeton sur hellostock.fr → Mon espace → Réglages → « Jetons d'API », puis colle-le dans oto. Il n'est affiché qu'une fois ; il commence par `hs_`.
- **un jeton par personne** : il porte les droits de SON compte, et ce qui est fait avec l'est en son nom (un envoi de demande est enregistré au nom de l'administrateur dont c'est le jeton). Pas de jeton d'org ni de clé oto partagée.
- **le compte doit être administrateur de la marketplace** : sinon HelloStock répond 403, et recréer un jeton n'y change rien — c'est le rôle du compte qui manque. Le rôle est relu à chaque appel : un compte rétrogradé passe au 403 sans que le jeton change.
- un jeton révoqué (ou inconnu) répond 401 : on en recrée un, puis on remplace l'ancien sur la carte.
- le bouton « tester la connexion » lit une demande : il éprouve les deux conditions d'un coup.

## usage — la revue hebdomadaire de la marketplace

- « les demandes de la semaine » → `hellostock_demande(since="2026-09-01")` ; une fiche entière (positionnements et envois compris) → `op="get"`
- « les offres sans mots-clés » → `hellostock_offre(certificat="sans-mots-cles")`, puis `op="get"` sur chacune : `certificat.verdictDetail` rend les valeurs lues sur le certificat
- « qui peut répondre à cette demande ? » → lire la demande, puis `hellostock_membre(service=…, sector=…)` et `hellostock_offre(matiere=…, departement=…)` ; c'est l'assistant qui propose les rapprochements, HelloStock n'en calcule aucun
- « qui s'est positionné ? » → `hellostock_positionnements(demande_id=…)`
- « envoie-la à ces trois fournisseurs » → `hellostock_demande_send(demande_id=…, user_ids=[…])` rend d'abord un **aperçu** ; l'envoi part avec `dry_run=False`
- « écris ces mots-clés » → `hellostock_offre_update(offre_id=…, keywords=[…])`
- « passe-la en publiée » → `hellostock_demande_set_status` / `hellostock_offre_update(status=…)`

les listes rendent `{items, nextCursor, total}` : `total` compte tout ce qui répond aux filtres, `cursor` reprend là où la page s'arrête. Elles sont projetées (les colonnes retirées sont nommées dans `projection`) ; `full=True` rend tout.

## note — ce que chaque écriture déclenche chez des tiers

- `hellostock_demande_send` **écrit à des personnes réelles**, depuis l'adresse de HelloStock : les specs de la demande (matière, nuance, format, dimensions, épaisseur, quantité, échéance, certificat), le mot d'accompagnement tel quel, et deux liens vers la page de la demande — jamais l'identité de l'acheteur, sa référence ni son commentaire. **Aperçu par défaut.** Un membre qui a déjà reçu la demande est refusé, sauf `allow_resend=True` : HelloStock, lui, renverrait sans rien dire.
- les liens du courriel ouvrent la page publique de la demande, qui n'affiche **que les demandes publiées** : envoyer une demande non publiée fait tomber les destinataires sur « Demande indisponible ». L'aperçu le signale.
- `noop: true` dans la réponse : la messagerie de HelloStock n'est pas configurée — **aucun courriel n'est parti**, et pourtant les envois sont enregistrés.
- `hellostock_demande_set_status` / `hellostock_offre_update(status=…)` : `published` est visible sur la marketplace publique **immédiatement**, tout autre statut l'en retire ; toutes les transitions sont permises. Aucun courriel.
- `hellostock_offre_update(keywords=…)` **remplace** la liste, et elle est **publique** (recherche). HelloStock normalise (minuscules, espaces, doublons) et refuse la liste entière si un terme nomme un aciériste, un n° de coulée ou de commande — y compris quand le certificat les montre. La réponse rend ce qui est réellement stocké.

## note — état de vérification

écrit sur le contrat OpenAPI 3.1 de l'API admin (`1.0.0`, servi par `GET /api/admin/openapi.json`) et vérifié contre lui : chaque route et chaque paramètre envoyés par le client existent dans le contrat, et les listes de valeurs (statuts, matières, certificat, secteurs) sont celles qu'il publie.

**éprouvé sur un faux serveur qui rejoue le contrat**, pas encore contre hellostock.fr : pagination, filtres refusés, 401/403, les trois écritures. **Non exercé en vrai** : tout ce qui dépend des données réelles (taille des champs libres, forme exacte d'un `verdictDetail`), et le courriel d'envoi. Le premier essai une fois un jeton posé : « tester la connexion », puis `hellostock_demande(limit=1)`.

**hors d'atteinte ici, à dessein** : supprimer un membre, modifier le contenu du site, télécharger le devis d'un positionnement. `certificat.url` pointe une page de session du site : elle ne s'ouvre pas avec le jeton.
