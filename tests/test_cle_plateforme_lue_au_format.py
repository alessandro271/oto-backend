"""A platform key is read like every other key: `unpack_secret`, no raw-value rescue.

`/api/admin/platform-keys` shapes the key at POSE time (`preparer_pose`). A platform key
of a multi-field connector that does not unpack was stored raw before that: the read
refuses it, naming the command that rewrites it, instead of guessing its field.
"""
import json

import pytest

from oto_mcp import credentials_store


def _resolue(provider: str, secret: str, is_platform: bool):
    from oto_mcp.access.resolved_credential import ResolvedCredential
    return ResolvedCredential(provider, secret, is_platform, "platform" if is_platform
                              else "byo_user", credentials_store.PLATFORM if is_platform
                              else "user", "oto" if is_platform else "u1")


def test_a_platform_key_in_vault_shape_unpacks():
    blob = json.dumps({"api_key": "fg-key", "account_id": "12"})
    assert _resolue("forager", blob, True).fields == {"api_key": "fg-key", "account_id": "12"}


def test_a_raw_platform_key_of_a_multi_field_connector_names_the_command():
    with pytest.raises(credentials_store.SecretUnpackError,
                       match="cles-plateforme-au-format --apply") as e:
        _resolue("forager", "fg-raw-key", True).fields
    assert "fg-raw-key" not in str(e.value)


def test_a_raw_byo_key_still_refuses_as_before():
    with pytest.raises(credentials_store.SecretUnpackError) as e:
        _resolue("forager", "fg-raw-key", False).fields
    assert "cles-plateforme-au-format" not in str(e.value)


def test_a_single_field_platform_key_is_its_raw_value():
    assert _resolue("serper", "sk-1", True).fields == \
        credentials_store.unpack_secret("serper", "sk-1")
