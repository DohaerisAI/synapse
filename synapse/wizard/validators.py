"""Pure validation functions for wizard inputs. Each returns None on success, error string on failure."""

from __future__ import annotations

import re


def non_empty(value: str) -> str | None:
    if not value.strip():
        return "This field cannot be empty."
    return None


def telegram_token(value: str) -> str | None:
    if not value.strip():
        return "Token cannot be empty."
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", value.strip()):
        return "Invalid format. Expected: <digits>:<alphanumeric>."
    return None


def port_number(value: str) -> str | None:
    if not value.strip():
        return "Port cannot be empty."
    try:
        port = int(value.strip())
    except ValueError:
        return "Port must be a number."
    if port < 1 or port > 65535:
        return "Port must be between 1 and 65535."
    return None


def positive_int(value: str) -> str | None:
    if not value.strip():
        return "Value cannot be empty."
    try:
        n = int(value.strip())
    except ValueError:
        return "Must be a number."
    if n < 1:
        return "Must be a positive number."
    return None


def url_format(value: str) -> str | None:
    if not value.strip():
        return None  # optional
    if not re.match(r"^https?://\S+$", value.strip()):
        return "Must be a valid URL (http:// or https://)."
    return None


def azure_endpoint(value: str) -> str | None:
    if not value.strip():
        return "Endpoint URL cannot be empty."
    if not re.match(r"^https://\S+$", value.strip()):
        return "Must be a valid HTTPS URL (e.g. https://myorg.openai.azure.com)."
    return None


def api_base_url(value: str) -> str | None:
    if not value.strip():
        return "API base URL cannot be empty."
    if not re.match(r"^https?://\S+$", value.strip()):
        return "Must be a valid URL (http:// or https://)."
    return None


def ip_or_hostname(value: str) -> str | None:
    if not value.strip():
        return "Host cannot be empty."
    return None
