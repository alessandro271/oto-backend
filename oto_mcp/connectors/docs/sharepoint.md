## prerequisite — une app Entra dans ton tenant Microsoft 365

le connecteur lit les fichiers de ton organisation avec une **app Entra** qu'un administrateur Microsoft 365 enregistre une fois, au nom de l'organisation (pas d'un collaborateur). Dans le [centre d'administration Entra](https://entra.microsoft.com) :
1. **Inscriptions d'applications → Nouvelle inscription** : un nom (ex. « oto »), « comptes de cet annuaire uniquement », sans URI de redirection
2. **Autorisations des API → Ajouter → Microsoft Graph → Autorisations d'application** (pas « déléguées ») :
   - lecture : `Sites.Read.All` (tous les sites SharePoint) et `Files.Read.All` (les OneDrive)
   - lecture et dépôt : `Sites.ReadWrite.All` et `Files.ReadWrite.All` à la place
   - ou `Sites.Selected`, pour n'ouvrir que les sites qu'un admin accorde un par un à l'app : le plus restreint
3. **Accorder le consentement administrateur** pour l'organisation : sans lui, chaque appel est refusé (403)
4. **Certificats et secrets → Nouveau secret client** : copie sa **valeur** tout de suite, elle n'est montrée qu'une fois

puis renseigne dans oto, sur la carte SharePoint de ton org : `directory_id` (ID de locataire, page « Vue d'ensemble »), `client_id` (ID d'application) et `client_secret` (la valeur du secret).
- byo_org : la clé vaut pour toute l'org, et elle voit **tout ce que ses permissions couvrent**. Ce n'est pas borné aux droits d'un collaborateur : choisis les permissions, ou `Sites.Selected`, en conséquence
- le bouton « tester la connexion » obtient un jeton : il vérifie le tenant, l'app et le secret, **pas les permissions** (avec `Sites.Selected`, une app saine ne voit encore aucun site)
- ⚠️ **le secret expire** (6 à 24 mois selon le choix fait à sa création) : à l'échéance, tout s'arrête avec « secret expiré ». En recréer un et le remplacer sur la carte

## usage — trouver, lire, déposer un document

- « le site Marketing » → `sharepoint_site(query="Marketing")`, ou par son adresse : `sharepoint_site(op="get", url="https://contoso.sharepoint.com/sites/Marketing")`
- « ses bibliothèques de documents » → `sharepoint_site(op="drives", site_id="…")` ; chaque `id` rendu est un `drive_id`
- « le contenu de ce dossier » → `sharepoint_file(drive_id="…", path="Contrats/2026")`
- « le OneDrive de Marie » → `sharepoint_file(user="marie@contoso.com")`, puis `path=` pour descendre
- « retrouve le contrat Dupont » → `sharepoint_file(op="search", drive_id="…", query="Dupont contrat")`
- « lis ce document » → `sharepoint_file(op="download", drive_id="…", item_id="…")` : un Word ou un PowerPoint revient en texte (converti en PDF par Microsoft), un Excel en CSV, un PDF en texte
- chaque réponse est une vue resserrée (nom, taille, type, dossier, lien, dernière modification) ; `full=true` rend l'objet Microsoft Graph complet
- « dépose ce compte rendu » → `sharepoint_file(op="upload", drive_id="…", path="Comptes rendus", name="cr-2026-10-01.md", content_text="…")` ; un fichier binaire en `content_base64`

## note — ce qui trompe

- ⚠️ **un 403 ne veut pas dire que le fichier n'existe pas** : l'app n'a pas la permission, le consentement admin manque, ou (avec `Sites.Selected`) ce site ne lui a pas été accordé
- ⚠️ **la recherche passe par l'index de SharePoint** : un fichier tout juste déposé peut ne pas y être encore. Pour le retrouver tout de suite, le lister par son dossier
- ⚠️ **un nom déjà pris est refusé** au dépôt (`conflict="fail"`, le défaut) : `conflict="rename"` garde les deux, `conflict="replace"` écrase
- la recherche de sites (`op="search"`) ne trouve que les sites que l'app voit, et pas les OneDrive personnels : pour ceux-là, `user=`

## note — périmètre

fichiers seulement : sites, bibliothèques, OneDrive ; lister, chercher, lire (jusqu'à 50 Mo), déposer (jusqu'à 25 Mo), créer un dossier. Rien ne supprime, ne déplace ni ne partage un fichier. Le courrier Outlook, le calendrier et Teams ne passent pas par ce connecteur.
