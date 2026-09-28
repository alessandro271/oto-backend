"""L'identité déclarée de l'instance — vérifiée au démarrage, jamais devinée.

oto-backend#968 (ADR 0070). Une instance qui ne déclare pas ses adresses, sa marque et
ses contrats ne démarre pas : c'est la classe (b) de l'inventaire
(`env_inventory.Classe.IDENTITE`). Ces valeurs avaient un défaut, et ce défaut était le
NÔTRE — domaine, dashboard, relais d'email, adresse d'expéditeur, boîte de contact, base
des invitations, origines CORS, contrats, site de marque. Une instance servie ailleurs
démarrait, l'air de fonctionner, en envoyant ses utilisateurs chez nous.

**Décision du 28/09/2026 : refus partout.** Toute instance, notre production comprise,
déclare ses valeurs ; en développement elles vont dans le `.env`. Il n'y a pas de
marqueur « instance tierce » : un tel marqueur serait lui-même un défaut à deviner.

Chaque lecture ci-dessous est l'accesseur que le code appelle au moment d'émettre le
lien, l'email ou le contrat — le démarrage rejoue donc exactement ce qui lèverait plus
tard, en une fois, et nomme TOUT ce qui manque plutôt que le premier trouvé.
`tests/test_identite_instance_968.py` vérifie que chaque variable de la classe (b) de
l'inventaire fait bien échouer ce contrôle quand elle manque.
"""
from __future__ import annotations

from typing import Callable


class IdentiteNonDeclaree(RuntimeError):
    """L'instance n'a pas déclaré tout ce qu'elle émet sous son nom."""


def _lectures() -> tuple[tuple[str, Callable[[], object]], ...]:
    # Imports tardifs : `config` ne dépend de rien, mais `legal_docs` tire la base et le
    # registre de tenants — ce module doit rester importable sans eux.
    from . import config, email, email_brand, legal_docs
    return (
        ("adresse publique de l'instance", config.public_base_url),
        ("domaine des projets publiés", config.project_domain),
        ("adresse du tableau de bord", config.dashboard_url),
        ("base des liens d'invitation", config.invite_base_url),
        ("origines CORS", config.cors_origins),
        ("relais d'email", email._mailer_url),
        ("expéditeur des emails", email._mail_from),
        ("boîte de contact", email._contact_to),
        ("documents légaux", legal_docs.current_docs),
        ("nom de la marque", email_brand.nom_instance),
        ("site de la marque", email_brand.site_instance),
    )


def verifier() -> None:
    """Refuse de démarrer si l'identité de l'instance n'est pas entièrement déclarée.

    Ne rattrape que `RuntimeError` — ce que lèvent `require_env` et les validations des
    accesseurs — pour les rassembler en UN refus ; toute autre exception est un défaut
    de code et remonte telle quelle."""
    manques = []
    for libelle, lire in _lectures():
        try:
            lire()
        except RuntimeError as exc:
            manques.append(f"- {libelle} : {exc}")
    if manques:
        raise IdentiteNonDeclaree(
            "cette instance ne déclare pas tout ce qu'elle émet sous son nom — elle ne "
            "démarre pas plutôt que de pointer chez quelqu'un d'autre :\n"
            + "\n".join(manques))
