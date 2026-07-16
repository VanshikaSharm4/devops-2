"""Tests for Splunk LDAP credential vault and username normalization."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from analysis.splunk_credentials import (
    delete_credentials,
    ldap_short_from_email,
    load_credentials,
    normalize_splunk_username,
    save_credentials,
)


def test_normalize_splunk_username_short():
    # Splunk basic-auth wants the BARE LDAP short name (confirmed: 'vanssharma'
    # → 200, 'vanssharma@adobe.com' → 401).
    assert normalize_splunk_username("vanssharma") == "vanssharma"
    assert normalize_splunk_username("  VanSharma  ") == "vansharma"


def test_normalize_splunk_username_full_email():
    # A pasted full email is stripped down to the bare short name.
    assert normalize_splunk_username("vanssharma@adobe.com") == "vanssharma"


def test_ldap_short_from_email():
    assert ldap_short_from_email("vanssharma@adobe.com") == "vanssharma"


def test_vault_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ARGUS_CREDENTIALS_KEY",
        "test-key-for-unit-tests-only-not-real=",
    )
    # Fernet needs valid 32-byte url-safe base64 key
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ARGUS_CREDENTIALS_KEY", key)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))

    vault_dir = tmp_path / ".splunk_vault"
    assert not vault_dir.exists()

    # Mock validation to avoid a live Splunk call. save_credentials() calls
    # check_splunk_credentials(), so that is what we patch.
    import analysis.splunk_credentials as sc

    monkeypatch.setattr(sc, "check_splunk_credentials", lambda u, p: (True, "ok"))

    email = save_credentials("testuser", "secret-pass")
    assert email == "testuser"
    assert vault_dir.exists()

    loaded = load_credentials("testuser")
    assert loaded is not None
    assert loaded[0] == "testuser"
    assert loaded[1] == "secret-pass"

    delete_credentials("testuser")
    assert load_credentials("testuser") is None


def test_splunk_auth_uses_contextvars(monkeypatch):
    monkeypatch.delenv("SPLUNK_USERNAME", raising=False)
    monkeypatch.delenv("SPLUNK_PASSWORD", raising=False)

    from analysis.customer_context import clear_splunk_context, set_splunk_context
    from connectors.splunk_connector import _auth

    clear_splunk_context()
    with pytest.raises(ValueError, match="Splunk credentials not configured"):
        _auth()

    set_splunk_context("testuser", "pass123")
    auth = _auth()
    assert auth.username == "testuser"
    assert auth.password == "pass123"
    clear_splunk_context()
