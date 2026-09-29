---
title: Instance cible — déployer le tronc sur une autre machine que la nôtre
type: how-to
---

# Instance cible — déployer le tronc sur une autre machine que la nôtre

> **Le fait à retenir** : une instance tierce est un **déploiement du tronc**, jamais une
> copie (ADR 0070). Elle tourne le même code, au tag qu'on choisit pour elle, avec la
> même bibliothèque bleu/vert que notre box — et tout ce qui la distingue de nous est
> une **déclaration**, rangée hors du dépôt. Ce dépôt est public : il ne nomme aucune
> cible, il ne porte que la forme de leur déclaration.

Issue d'origine : #967 (« une chaîne de livraison, N cibles »). Les deux instances sont
**autonomes** : déployer une cible est une décision de montée de version prise à part,
jamais la suite automatique d'un tag. Le workflow d'une cible ne consulte qu'elle-même
et le tronc : aucun autre déploiement, aucune autre instance, aucun autre run n'entre
dans sa décision, et rien d'autre ne l'appelle.

## La bibliothèque bleu/vert, commune

`deploy/oto-mcp-bluegreen.sh` sert nos deux environnements **et** chaque cible. Tout ce
qu'elle codait en dur pour notre box est devenu une variable que le wrapper **déclare** :
verrou (`BG_LOCK`), Caddyfile validé avant chaque reload (`BG_CADDYFILE`), script et
unité de vidange (`BG_DRAIN`, `BG_DRAIN_UNIT`), mode du lanceur (`BG_LANCEUR`), en plus
des ports, arbres, amont et couleur qu'elle recevait déjà. **Aucune n'a de défaut** : une
variable oubliée refuse le chargement en la nommant, et un pointeur de couleur absent est
une panne nommée, plus une invitation à supposer « bleu ».

**Notre box fait exactement les mêmes gestes qu'avant.** Nos deux wrappers
(`deploy/oto-backend.sh`, `deploy/oto-backend-canari.sh`) déclarent les valeurs qui
étaient en dur, à l'identique. La preuve est un banc, pas une relecture :
`tests/deploy/test_gestes_bleu_vert_967.py` rejoue chaque scénario réel — bascule dans
les deux sens, retour arrière, préproduction, vidange, couleur morte, `caddy validate`
qui refuse, trafic public en échec, lanceur figé — avec des doublures qui journalisent
chaque commande, et compare la trace (commandes, sortie, état final des fichiers) à la
référence enregistrée depuis les scripts d'avant (`tests/deploy/gestes_bleu_vert/`).
Un geste de notre box qui change fait rougir ce test ; le changer exprès, c'est
régénérer la référence (`tests/deploy/_banc_bleu_vert.py`) et relire son diff.

⚠️ Le dépôt ne se propage pas seul sur notre box : `/opt/deploy/` y est une **copie**.
Un changement de la bibliothèque n'y agit qu'une fois recopié (infra, sur go d'Alexis).
Comparer la box au dépôt se fait sur le code, en-têtes retirés :
`grep -v '^#' <fichier> | sha256sum` des deux côtés.

## Le lanceur, générique

`deploy/lanceur_secrets.py` démarre le serveur d'une cible (`BG_LANCEUR=versionne`). Il
est **dans le tag** : ni propagé, ni édité sur la machine.

- **Ce qu'il tire se dérive de l'inventaire** : `oto_mcp/env_secrets.py` nomme, parmi
  les variables de `env_inventory.py`, celles qui sont des secrets. Les requis sont tirés
  à chaque démarrage ; les facultatifs, l'instance les **déclare**
  (`OTO_SECRETS_OPTIONNELS`). Un secret introuvable refuse le démarrage.
- **Où il le tire se déclare** : le Secret Manager du projet Scaleway **de la cible**
  (`OTO_SECRETS_REGION`, `OTO_SECRETS_PROJET`), sous le chemin de son rôle
  (`OTO_SECRETS_CHEMIN`, `/preprod` ou `/prod` : chaque rôle a sa `DATABASE_URL`). Chaque
  secret porte le **nom de sa variable**. Aucun identifiant n'est écrit nulle part.
- **La clé d'API** arrive par `LoadCredential=scw:…` de l'unité, lisible du seul service.
- **Le `.env` ne porte que le non-secret** : un secret trouvé dans l'environnement
  refuse le démarrage.

Notre box garde son lanceur (`BG_LANCEUR=propage`) : `deploy/start-encrypted.sh` et
`-canari.sh` sont la déclaration de secrets **propre à notre cible** — identifiants de
notre projet, Mollie, Pennylane. La passer au lanceur générique est un geste à part
(créer nos secrets par nom, vider le `.env` de ses secrets, poser l'unité), pas une
conséquence de ce chantier.

## L'amorce — une instance naît du code

`deploy/cible/amorcer.sh` fait naître un rôle (préprod ou prod) sur une machine nue, et
le remet à sa forme déclarée à chaque déploiement : c'est le déploiement qui l'appelle,
depuis l'arbre du tag, avant la bascule. Idempotente — la seconde fois, elle ne recrée
rien et réécrit seulement ce qui se dérive de la déclaration.

| Ce qu'elle pose | Où (dérivé du nom d'instance `<i>` et du rôle `<r>`) |
|---|---|
| Utilisateur de service, système, sans shell — **jamais root** | `oto-<i>` |
| Python du plancher du `pyproject` (3.10), via uv | `/opt/<i>/python` |
| Deux arbres (clone du tronc) et leur venv | `/opt/<i>/<r>-blue`, `/opt/<i>/<r>-green` |
| Environnement du rôle : `app.env` (non-secret), `lanceur.env`, ports | `/etc/<i>/<r>/` (0700, root) |
| Pointeur de couleur, posé à la naissance seulement (bleu : le 1er déploiement installe la verte) | `/etc/<i>/<r>/active` |
| Unité par couleur, confinée (`NoNewPrivileges`, `ProtectSystem`, `CapabilityBoundingSet=`, lien-local refusé) | `<i>-<r>@.service` (gabarit `deploy/cible/instance@.service`) |
| Amont Caddy initial | `/etc/caddy/upstream-<i>-<r>.conf` |
| Vidange (chemin stable : elle tourne après le déploiement) | `/usr/local/lib/<i>/oto-mcp-drain.sh` |
| Maintenance quotidienne, après la bascule, sur l'arbre qui sert — chaque rôle a sa base, donc la sienne | `<i>-<r>-maintenance.{service,timer}` |

Ce qu'elle **exige et ne pose jamais** — le socle, geste d'opérateur, chacun refusé en
le nommant s'il manque : root ; `uv`, `git`, `caddy`, `python3` ; la clé d'API du
Secret Manager dans `/etc/<i>/scw.key` (0600) ; un Caddyfile qui importe l'amont du rôle
et l'utilise dans le bloc du site :

```
import /etc/caddy/upstream-<i>-<r>.conf          # en tête, avant le bloc global

<hôte public du rôle> {
	handle /p/d/* {
		import <i>_<r>_upstream_docshare
	}
	import <i>_<r>_upstream
}
```

Le service ne peut pas réécrire son code (les arbres sont à root), n'a d'état que dans
son `StateDirectory`, et ne voit sa clé d'API que par `LoadCredential`.

## La chaîne — monter une cible de version

`.github/workflows/deploy-cible.yml`, **à la main seulement** (onglet Actions ou
`gh workflow run deploy-cible.yml -f cible=<environnement> -f tag=vX.Y.Z -f etape=…`) :

1. **Entrées** validées avant tout usage (elles finissent dans un `ref:` et un nom
   d'environnement), puis **l'environnement de la cible doit exister et exiger un
   relecteur** (`deploy/cible/protection.sh`, par l'API de GitHub) : sinon, refus.
2. **Approbation** par le relecteur requis — un seul job derrière elle, qui porte toute
   la montée demandée : une montée, une décision.
3. **Le tag est sur la branche principale du tronc** (la porte le revérifie sur la
   machine).
4. **La déclaration** est jugée par l'inventaire du tag.
5. **Compatibilité inverse, facultative** : si l'environnement de la cible déclare un
   consommateur de son API (`CIBLE_CONSOMMATEUR`), le contrat que **ce tag** servirait
   chez elle est confronté au contrat qu'il épingle (`scripts/contrat-front.py`). Le tronc
   ne s'interdit rien pour une cible ; c'est la cible qui juge le tag au moment de
   monter. Contrat illisible = pas de montée.
6. **Préprod** de la cible, puis constat de ce qu'elle sert (`GET /api/version`).
7. **Prod** de la cible — seulement si sa préprod **sert déjà** ce tag, constaté de
   l'extérieur. `etape=prod` seul est donc le geste du lendemain.

Un test (`tests/test_workflow_deploy_cible_967.py`) rougit si ce workflow, ou un script
qu'il exécute, se met à consulter un autre déploiement que celui de la cible.

`action=retour` rebascule un rôle sur sa couleur précédente, sans rien installer.

**L'accès** : runner hébergé par GitHub → `cloudflared` (dépôt APT signé) → tunnel
Cloudflare Access de la cible, authentifié par **jeton de service** → SSH par la clé de
déploiement, hôte **épinglé** → **commande forcée** vers la porte
(`deploy/cible/appeler.sh`). La déclaration part sur l'entrée standard.

**La porte** (`deploy/cible/porte.sh`, posée une fois sur la machine à
`/usr/local/sbin/oto-cible-porte`) est le seul fichier de la chaîne qui y vit. Elle
n'accepte que `deployer|retour <preprod|prod> <vX.Y.Z>`, vérifie que le tag existe et
qu'il est **sur la branche principale du tronc** (miroir local du dépôt, dont l'URL est
écrite dans la porte et jamais reçue), extrait le `deploy/` **de ce tag** et lui passe
la main (`deploy/cible/deployer.sh` : amorce, bleu/vert, maintenance). L'amorce, la
bibliothèque et le lanceur sont donc toujours ceux de la version qu'on monte.

## Hors de ce dépôt : les workers runner

Les workers `oto-runner` vivent dans leur propre dépôt et y suivent `main`. Pour une
cible, ils doivent tourner **au même tag** que son back-end : il faudra, dans ce dépôt-là,
une montée pilotée par tag (même déclenchement manuel, même approbation), et une
déclaration de l'URL du back-end de la cible. Rien de ce chantier ne le fait.
