"""Tests for WhatsApp environment configuration loading and validation."""

import dataclasses

import pytest

import interfaces.whatsapp.config as config_module
from interfaces.whatsapp.config import WhatsAppConfigError, load_whatsapp_config

_VALID_ENV = {
    "WHATSAPP_VERIFY_TOKEN": "verify-token-123",
    "WHATSAPP_APP_SECRET": "app-secret-456",
    "WHATSAPP_ACCESS_TOKEN": "access-token-789",
    "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
    "WHATSAPP_AUTHORIZED_SENDER_ID": "15551234567",
    "WHATSAPP_API_VERSION": "v23.0",
}


def test_loads_a_fully_valid_environment():
    config = load_whatsapp_config(dict(_VALID_ENV))

    assert config.verify_token == "verify-token-123"
    assert config.app_secret == "app-secret-456"
    assert config.access_token == "access-token-789"
    assert config.phone_number_id == "1234567890"
    assert config.authorized_sender_id == "15551234567"
    assert config.api_version == "v23.0"
    assert config.host == "127.0.0.1"
    assert config.port == 8000


@pytest.mark.parametrize("missing", [
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_AUTHORIZED_SENDER_ID",
    "WHATSAPP_API_VERSION",
])
def test_missing_required_variable_raises(missing):
    env = dict(_VALID_ENV)
    del env[missing]

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_blank_required_variable_raises():
    env = dict(_VALID_ENV)
    env["WHATSAPP_VERIFY_TOKEN"] = "   "

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


# --- Single authorized sender (no allow-list) -----------------------------


def test_allowed_senders_is_not_a_supported_variable():
    # A legacy WHATSAPP_ALLOWED_SENDERS value must have no effect - the
    # single required variable is WHATSAPP_AUTHORIZED_SENDER_ID, and its
    # absence must still fail even if the legacy variable is present.
    env = dict(_VALID_ENV)
    del env["WHATSAPP_AUTHORIZED_SENDER_ID"]
    env["WHATSAPP_ALLOWED_SENDERS"] = "15551234567,15557654321"

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_authorized_sender_id_does_not_accept_comma_separated_values():
    env = dict(_VALID_ENV)
    env["WHATSAPP_AUTHORIZED_SENDER_ID"] = "15551234567,15557654321"

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_authorized_sender_id_rejects_non_digit_characters():
    env = dict(_VALID_ENV)
    env["WHATSAPP_AUTHORIZED_SENDER_ID"] = "+15551234567"

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_authorized_sender_id_is_stored_exactly_with_no_normalization():
    env = dict(_VALID_ENV)
    env["WHATSAPP_AUTHORIZED_SENDER_ID"] = "015551234567"

    config = load_whatsapp_config(env)

    assert config.authorized_sender_id == "015551234567"


# --- API version: required, no code default -------------------------------


def test_api_version_has_no_default_and_is_required():
    env = dict(_VALID_ENV)
    del env["WHATSAPP_API_VERSION"]

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_api_version_does_not_default_to_a_real_meta_version():
    # Guards against reintroducing a hardcoded real Graph API version as a
    # fallback: an empty/missing value must always raise, never silently
    # resolve to something like "v20.0".
    env = dict(_VALID_ENV)
    env["WHATSAPP_API_VERSION"] = ""

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


@pytest.mark.parametrize("malformed", ["20.0", "v20", "vNext", "v20.0.1", "latest"])
def test_malformed_api_version_raises(malformed):
    env = dict(_VALID_ENV)
    env["WHATSAPP_API_VERSION"] = malformed

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_well_formed_api_version_is_accepted():
    env = dict(_VALID_ENV)
    env["WHATSAPP_API_VERSION"] = "v99.5"

    config = load_whatsapp_config(env)

    assert config.api_version == "v99.5"


# --- Host: defaults to loopback, rejects anything else ---------------------


def test_host_defaults_to_127_0_0_1():
    config = load_whatsapp_config(dict(_VALID_ENV))

    assert config.host == "127.0.0.1"


@pytest.mark.parametrize("valid_host", ["127.0.0.1", "127.0.0.5", "localhost", "::1"])
def test_loopback_hosts_are_accepted(valid_host):
    env = dict(_VALID_ENV)
    env["WHATSAPP_HOST"] = valid_host

    config = load_whatsapp_config(env)

    assert config.host == valid_host


@pytest.mark.parametrize("invalid_host", [
    "0.0.0.0",
    "192.168.1.10",   # LAN address
    "10.0.0.5",       # LAN address
    "8.8.8.8",        # public address
    "example.com",    # arbitrary hostname, not resolved, not "localhost"
])
def test_non_loopback_hosts_are_rejected(invalid_host):
    env = dict(_VALID_ENV)
    env["WHATSAPP_HOST"] = invalid_host

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


# --- Port -------------------------------------------------------------


def test_custom_port_is_used():
    env = dict(_VALID_ENV)
    env["WHATSAPP_PORT"] = "9001"

    config = load_whatsapp_config(env)

    assert config.port == 9001


def test_invalid_port_raises():
    env = dict(_VALID_ENV)
    env["WHATSAPP_PORT"] = "not-a-number"

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


def test_out_of_range_port_raises():
    env = dict(_VALID_ENV)
    env["WHATSAPP_PORT"] = "70000"

    with pytest.raises(WhatsAppConfigError):
        load_whatsapp_config(env)


# --- WhatsAppConfig contract: frozen dataclass, secrets excluded from repr -


def test_config_is_frozen():
    config = load_whatsapp_config(dict(_VALID_ENV))

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.port = 9999


def test_config_is_a_dataclass():
    assert dataclasses.is_dataclass(config_module.WhatsAppConfig)


def test_repr_excludes_secrets_and_personal_identifiers():
    config = load_whatsapp_config(dict(_VALID_ENV))
    rendered = repr(config)

    for secret_value in (
        config.verify_token,
        config.app_secret,
        config.access_token,
        config.phone_number_id,
        config.authorized_sender_id,
    ):
        assert secret_value not in rendered


def test_repr_still_shows_host_port_and_api_version():
    config = load_whatsapp_config(dict(_VALID_ENV))
    rendered = repr(config)

    assert config.host in rendered
    assert str(config.port) in rendered
    assert config.api_version in rendered


def test_validation_errors_never_include_secret_values():
    env = dict(_VALID_ENV)
    env["WHATSAPP_AUTHORIZED_SENDER_ID"] = "not-digits-12345"

    with pytest.raises(WhatsAppConfigError) as exc_info:
        load_whatsapp_config(env)

    assert "not-digits-12345" not in str(exc_info.value)


def test_load_dotenv_uses_override_false(monkeypatch, tmp_path):
    captured = {}

    def fake_load_dotenv(*args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(config_module, "load_dotenv", fake_load_dotenv)
    for name, value in _VALID_ENV.items():
        monkeypatch.setenv(name, value)

    load_whatsapp_config()  # env=None forces the load_dotenv() call path

    assert captured.get("override") is False
