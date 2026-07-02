"""Tests for app.social.config — env-gated, default-OFF (mirrors app.cloud_state pattern).

The whole feature is inert until SOCIAL_POSTING_ENABLED is truthy AND the secrets are set, so
prod stays untouched while the backend lies dormant.
"""

from __future__ import annotations

import pytest

from app.social import config


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOCIAL_POSTING_ENABLED", raising=False)
    assert config.social_posting_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes"])
def test_enabled_when_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("SOCIAL_POSTING_ENABLED", val)
    assert config.social_posting_enabled() is True


def test_token_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOCIAL_TOKEN_KEY", "abc")
    assert config.token_key() == "abc"


def test_youtube_oauth_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "sec")
    assert config.youtube_oauth() == ("cid", "sec")
