"""Déclaration de registre du connecteur `instagram_meta` — les STATISTIQUES d'un
compte Instagram professionnel, par l'API officielle de Meta.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# ⚠️ **Deux connecteurs Instagram, et le nom dit lequel.** `instagram` (sans
# suffixe) est la MESSAGERIE du compte, opérée par Unipile ; celui-ci est la
# STATISTIQUE, servie par l'API officielle de Meta (« Instagram API with Instagram
# Login »). Ils ne sont pas substituables : ni la même source, ni le même
# credential, ni les mêmes droits — c'est exactement le cas que l'ADR 0010
# §Amendement du 2026-08-10 nomme, « le namespace porte la CAPACITÉ, suffixée du
# FOURNISSEUR quand plusieurs fournisseurs non substituables la rendent », et dont
# `linkedin_unipile_*` / `linkedin_aiark_*` est le précédent.
#
# `tool_visibility.namespace_of` résout au plus long préfixe DÉCLARÉ : un
# `instagram_meta_get_profile` tombe donc sur CE connecteur et pas sur l'autre —
# gate d'appel, ACL, sélection et visibilité restent distincts. Verrouillé par
# `tests/test_instagram_meta_natif.py`.
#
# ⚠️ La symétrie est imparfaite et le reste : le couple doctrinal serait
# `instagram_unipile` / `instagram_meta`. Renommer l'existant coûterait un contrat
# consommé HORS de ce dépôt (`instagram_chat` est appelé par des procédures en base
# et des guides) — c'est la raison même pour laquelle le split Unipile du
# 2026-08-28 n'a touché aucun nom d'outil. On ajoute le suffixe au nouveau venu,
# on ne le rétrofite pas sur l'ancien.
#
# ⚠️ **Sans App Review de Meta, ce connecteur ne sert que les comptes INVITÉS comme
# testeurs sur notre application.** Ce n'est pas une limite de code : c'est le
# régime des applications Meta non publiées. Une personne qui s'inscrit seule
# obtient un refus au consentement, et c'est ce que la fiche annonce AVANT et ce
# que le message d'erreur nomme APRÈS.
CONNECTOR = _c(
    "instagram_meta", ["instagram_meta"],
    auth_modes={"byo_user"},
    # Le consentement Instagram EST personnel : il naît du compte de la personne,
    # pas de son appartenance à une org. Même famille que `google`.
    personal_session=True, secret_kind="oauth",
    label="Instagram (statistiques)",
    help="Les statistiques de ton compte Instagram professionnel — profil, "
         "publications, portée, interactions. Tu autorises oto chez Instagram ; "
         "aucune Page Facebook n'est nécessaire. Lecture seule.",
    href="https://www.instagram.com",
)

CATEGORY = "Métier"
# Qui reçoit l'appel (`docs/connector-vault.md` §« Ce que la fiche DIT ») : Meta.
# C'est SON API officielle, SON dialogue de consentement, et c'est elle qui décide
# ce que le jeton ouvre. Le contraste avec `planity` — déclaré « Otomata » parce
# que le connecteur y rejoue une session faute d'API publique — est le fond de la
# règle, pas une exception à celle-ci.
PUBLISHER = "Meta"
LOGO_DOMAIN = "instagram.com"

DESCRIPTION = (
    "Les statistiques d'un compte Instagram professionnel (Business ou Créateur) "
    "par l'API officielle de Meta : profil et abonnés, publications récentes, "
    "insights d'une publication et du compte, plus un calcul des meilleures heures "
    "de publication. Lecture seule — rien n'est publié, modifié ni supprimé. Tu "
    "connectes ton compte Instagram directement : aucune Page Facebook n'est "
    "requise. Ce sont les statistiques, pas les messages : les DM ont leur propre "
    "connecteur."
)
