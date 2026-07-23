"""
Environment configuration for the WhatsApp interface.

Everything the WhatsApp server needs beyond the kernel's own Config
(kernel/config/config.py) - Cloud API credentials, the webhook shared
secret, the single authorized sender, and the small set of operational
settings specific to this interface. Loaded and validated in one place so
a misconfigured deployment fails fast, at startup, with a clear message,
rather than partway through handling a webhook request.

This interface supports exactly one authorized user. There is no
allow-list, no multi-user support, and no sender-ID normalization -
authorization is a single exact string comparison.
"""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Resolved relative to this file, not the current working directory, the
# same way kernel/config/config.py resolves .env - so this behaves the
# same no matter where the server is started from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

_DEFAULT_PORT = 8000
_DEFAULT_HOST = "127.0.0.1"

# Meta's Cloud API version format, e.g. "v23.0". There is no code default -
# WHATSAPP_API_VERSION is required, and this only validates its shape.
_API_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")

# Hostnames (as opposed to IP literals) that are accepted as loopback
# without a DNS lookup. Anything else must be a literal IP address whose
# ipaddress.is_loopback is True.
_LOOPBACK_HOSTNAMES = {"localhost"}


class WhatsAppConfigError(ValueError):
    """Raised when required WhatsApp environment configuration is missing or invalid."""


@dataclass(frozen=True)
class WhatsAppConfig:
    """Validated environment configuration for the WhatsApp interface.

    Frozen so a composition root can't accidentally mutate it after
    validation. Secrets and personal identifiers are excluded from
    repr() - only host, port, and api_version (none of which are secret
    or personally identifying) are visible if this object is ever logged
    or printed by accident.
    """

    verify_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    phone_number_id: str = field(repr=False)
    authorized_sender_id: str = field(repr=False)
    host: str
    port: int
    api_version: str


def _require(env, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise WhatsAppConfigError(f"missing required environment variable: {name}")
    return value


def _parse_authorized_sender_id(env) -> str:
    raw = _require(env, "WHATSAPP_AUTHORIZED_SENDER_ID")
    if not raw.isdigit():
        # Deliberately does not echo the invalid value back - it's a
        # personal phone number, not just a formatting mistake like a port
        # or API version.
        raise WhatsAppConfigError(
            "WHATSAPP_AUTHORIZED_SENDER_ID must be digits only "
            "(WhatsApp wire format, no '+' or separators)"
        )
    return raw


def _parse_api_version(env) -> str:
    raw = _require(env, "WHATSAPP_API_VERSION")
    if not _API_VERSION_PATTERN.match(raw):
        raise WhatsAppConfigError(
            f"WHATSAPP_API_VERSION must look like 'vNN.N' (e.g. 'v23.0'), got: {raw!r}. "
            "Verify the currently supported Cloud API version before setting this."
        )
    return raw


def _parse_host(env) -> str:
    raw = (env.get("WHATSAPP_HOST") or "").strip()
    host = raw or _DEFAULT_HOST

    if host in _LOOPBACK_HOSTNAMES:
        return host

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise WhatsAppConfigError(
            f"WHATSAPP_HOST must be a loopback address or 'localhost', got: {host!r}"
        )

    if not address.is_loopback:
        raise WhatsAppConfigError(
            f"WHATSAPP_HOST must be loopback-only (127.0.0.0/8 or ::1), got "
            f"non-loopback address: {host!r}"
        )

    return host


def _parse_port(env) -> int:
    raw = (env.get("WHATSAPP_PORT") or "").strip()
    if not raw:
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise WhatsAppConfigError(f"WHATSAPP_PORT must be an integer, got: {raw!r}")
    if not (1 <= port <= 65535):
        raise WhatsAppConfigError(f"WHATSAPP_PORT must be between 1 and 65535, got: {port}")
    return port


def load_whatsapp_config(env: dict | None = None) -> WhatsAppConfig:
    """Load and validate WhatsApp environment configuration.

    Accepts an explicit `env` mapping for testing. When omitted, loads
    `.env` (same as kernel/config/config.py) and reads from the real
    process environment.
    """

    if env is None:
        # override=False is explicit (not just the library default): a
        # real process environment variable must always win over .env,
        # consistent with kernel/config/config.py's load_dotenv() call.
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
        env = os.environ

    return WhatsAppConfig(
        verify_token=_require(env, "WHATSAPP_VERIFY_TOKEN"),
        app_secret=_require(env, "WHATSAPP_APP_SECRET"),
        access_token=_require(env, "WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=_require(env, "WHATSAPP_PHONE_NUMBER_ID"),
        authorized_sender_id=_parse_authorized_sender_id(env),
        host=_parse_host(env),
        port=_parse_port(env),
        api_version=_parse_api_version(env),
    )
