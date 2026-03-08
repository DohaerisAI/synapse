from __future__ import annotations

import pytest

from synapse.wizard.validators import (
    api_base_url,
    azure_endpoint,
    ip_or_hostname,
    non_empty,
    port_number,
    positive_int,
    telegram_token,
    url_format,
)


class TestNonEmpty:
    def test_valid(self):
        assert non_empty("hello") is None

    def test_empty(self):
        assert non_empty("") is not None

    def test_whitespace_only(self):
        assert non_empty("   ") is not None

    def test_with_spaces(self):
        assert non_empty("  hi  ") is None


class TestTelegramToken:
    def test_valid(self):
        assert telegram_token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11") is None

    def test_valid_simple(self):
        assert telegram_token("12345:abcdef") is None

    def test_empty(self):
        assert telegram_token("") is not None

    def test_missing_colon(self):
        assert telegram_token("123456abcdef") is not None

    def test_no_digits_prefix(self):
        assert telegram_token("abc:def123") is not None

    def test_special_chars(self):
        assert telegram_token("123:abc!@#") is not None

    def test_whitespace_stripped(self):
        assert telegram_token("  12345:abcdef  ") is None


class TestPortNumber:
    def test_valid(self):
        assert port_number("8000") is None

    def test_min(self):
        assert port_number("1") is None

    def test_max(self):
        assert port_number("65535") is None

    def test_zero(self):
        assert port_number("0") is not None

    def test_too_high(self):
        assert port_number("65536") is not None

    def test_negative(self):
        assert port_number("-1") is not None

    def test_not_a_number(self):
        assert port_number("abc") is not None

    def test_empty(self):
        assert port_number("") is not None

    def test_whitespace_stripped(self):
        assert port_number("  8080  ") is None


class TestPositiveInt:
    def test_valid(self):
        assert positive_int("10") is None

    def test_one(self):
        assert positive_int("1") is None

    def test_zero(self):
        assert positive_int("0") is not None

    def test_negative(self):
        assert positive_int("-5") is not None

    def test_not_a_number(self):
        assert positive_int("xyz") is not None

    def test_empty(self):
        assert positive_int("") is not None


class TestUrlFormat:
    def test_http(self):
        assert url_format("http://example.com") is None

    def test_https(self):
        assert url_format("https://example.com/path") is None

    def test_empty_is_optional(self):
        assert url_format("") is None

    def test_no_scheme(self):
        assert url_format("example.com") is not None

    def test_ftp(self):
        assert url_format("ftp://files.example.com") is not None


class TestAzureEndpoint:
    def test_valid_https(self):
        assert azure_endpoint("https://myorg.openai.azure.com") is None

    def test_valid_with_path(self):
        assert azure_endpoint("https://myorg.openai.azure.com/openai") is None

    def test_http_rejected(self):
        assert azure_endpoint("http://myorg.openai.azure.com") is not None

    def test_empty(self):
        assert azure_endpoint("") is not None

    def test_no_scheme(self):
        assert azure_endpoint("myorg.openai.azure.com") is not None

    def test_whitespace_stripped(self):
        assert azure_endpoint("  https://myorg.openai.azure.com  ") is None


class TestApiBaseUrl:
    def test_https(self):
        assert api_base_url("https://api.example.com/v1") is None

    def test_http(self):
        assert api_base_url("http://localhost:8080") is None

    def test_empty(self):
        assert api_base_url("") is not None

    def test_no_scheme(self):
        assert api_base_url("api.example.com") is not None


class TestIpOrHostname:
    def test_localhost(self):
        assert ip_or_hostname("127.0.0.1") is None

    def test_hostname(self):
        assert ip_or_hostname("myhost.local") is None

    def test_empty(self):
        assert ip_or_hostname("") is not None

    def test_whitespace(self):
        assert ip_or_hostname("   ") is not None
