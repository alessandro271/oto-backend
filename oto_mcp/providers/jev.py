"""Registry declaration for the `jev` connector (aggregated by `providers/__init__.py`)."""
from __future__ import annotations

from ._model import CredentialField, _c

# Jev: TypeSafe's typed-answer model, served through OpenRouter. Named after the model,
# not the router: going direct to TypeSafe only changes the client's base URL.
#
# ⚠️ TENANT key only: no platform key, no personal key. `byo_org` is declared only
# because `require_credential("tenant", …)` needs it; a closer key (org/team/user) is
# refused at call time (`tools/jev.py::_client`). No free tier.
CONNECTOR = _c(
    "jev", ["jev"],
    auth_modes={"byo_org"}, keyed=True,
    secret_kind="api_key",
    # Mono: one call uses ONE key; two keys on the same rung could not be told apart.
    cardinality="mono",
    platform_key_open=False,
    label="Jev",
    help="Typed answers with probabilities (yes/no, choice, scale). OpenRouter key, set by a "
         "tenant admin.",
    href="https://openrouter.ai/settings/keys",
    # Named field: the key is an OpenRouter key (TypeSafe issues none).
    credential_fields=(
        CredentialField("key", "OpenRouter API key", secret=True,
                        help="Starts with `sk-or-`. Use a dedicated key with a spend cap."),
    ),
)

CATEGORY = "Modèles"
PUBLISHER = "TypeSafe"
LOGO_DOMAIN = "typesafe.ai"

DESCRIPTION = (
    "Jev answers closed questions about a state you give it: yes/no with a probability, "
    "a choice among options, or a position on a scale. No text, just typed answers. "
    "Built for triaging many rows or profiles with one rubric. The state goes to a third "
    "party (OpenRouter, then TypeSafe): send only the fields the judgement needs. Runs on "
    "the key set by the tenant."
)
