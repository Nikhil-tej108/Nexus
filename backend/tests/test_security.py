"""
Unit tests for app/security.py

Covers:
* password hashing and verification (hash_password / verify_password)
* JWT token creation and decoding (create_token / decode_token)
* CSRF enforcement (require_csrf)
* cookie naming helpers
* token_user_from_payload round-trip
* cache_key determinism and collision-resistance
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.models import Role
from app.schemas import TokenUser
from app.security import (
    cache_key,
    cookie_name,
    create_token,
    decode_token,
    hash_password,
    new_csrf,
    require_csrf,
    token_user_from_payload,
    verify_password,
)


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_returns_non_empty_string(self):
        h = hash_password("MySecret123!")
        assert isinstance(h, str) and len(h) > 20

    def test_verify_correct_password(self):
        pw = "CorrectHorse!Battery99"
        assert verify_password(pw, hash_password(pw)) is True

    def test_verify_wrong_password(self):
        h = hash_password("RightPassword1!")
        assert verify_password("WrongPassword1!", h) is False

    def test_different_hashes_same_password(self):
        """Argon2 uses random salts – two hashes of the same pw must differ."""
        pw = "SamePassword1!"
        assert hash_password(pw) != hash_password(pw)

    def test_verify_empty_string_fails(self):
        h = hash_password("ValidPassword1!")
        assert verify_password("", h) is False


# ---------------------------------------------------------------------------
# JWT token lifecycle
# ---------------------------------------------------------------------------

class TestJWTToken:
    def _make_token_user(self, role: Role = Role.internal) -> TokenUser:
        return TokenUser(
            id=uuid.uuid4(),
            email="user@test.local",
            role=role,
            must_change_password=False,
        )

    def test_create_and_decode_roundtrip(self):
        tu = self._make_token_user()
        token = create_token(tu, "chat")
        payload = decode_token(token)
        assert payload["email"] == tu.email
        assert payload["role"] == tu.role.value
        assert str(tu.id) == payload["sub"]

    def test_must_change_password_included(self):
        tu = TokenUser(
            id=uuid.uuid4(),
            email="x@test.local",
            role=Role.internal,
            must_change_password=True,
        )
        token = create_token(tu, "chat")
        payload = decode_token(token)
        assert payload["mcp"] is True

    def test_site_included_in_payload(self):
        tu = self._make_token_user(Role.admin)
        token = create_token(tu, "admin")
        payload = decode_token(token)
        assert payload["site"] == "admin"

    def test_decode_invalid_token_raises_401(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            decode_token("totally.invalid.token")
        assert exc_info.value.status_code == 401

    def test_decode_tampered_token_raises_401(self):
        from fastapi import HTTPException
        tu = self._make_token_user()
        token = create_token(tu, "chat")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(HTTPException):
            decode_token(tampered)


# ---------------------------------------------------------------------------
# token_user_from_payload
# ---------------------------------------------------------------------------

class TestTokenUserFromPayload:
    def test_roundtrip(self):
        tu = TokenUser(
            id=uuid.uuid4(),
            email="round@test.local",
            role=Role.external,
            must_change_password=False,
        )
        token = create_token(tu, "chat")
        payload = decode_token(token)
        result = token_user_from_payload(payload)
        assert result.id == tu.id
        assert result.email == tu.email
        assert result.role == tu.role

    def test_mcp_defaults_to_false_if_missing(self):
        payload = {
            "sub": str(uuid.uuid4()),
            "email": "x@test.local",
            "role": "internal",
        }
        result = token_user_from_payload(payload)
        assert result.must_change_password is False


# ---------------------------------------------------------------------------
# CSRF enforcement
# ---------------------------------------------------------------------------

class TestRequireCSRF:
    def _make_request(self, method: str, cookie: str = "", header: str = "") -> MagicMock:
        req = MagicMock()
        req.method = method
        req.cookies = {"nexus_csrf": cookie} if cookie else {}
        req.headers = {"X-CSRF-Token": header} if header else {}
        return req

    def test_get_request_passes_without_csrf(self):
        req = self._make_request("GET")
        require_csrf(req)  # should not raise

    def test_head_request_passes_without_csrf(self):
        req = self._make_request("HEAD")
        require_csrf(req)

    def test_options_request_passes_without_csrf(self):
        req = self._make_request("OPTIONS")
        require_csrf(req)

    def test_post_with_matching_csrf_passes(self):
        token = new_csrf()
        req = MagicMock()
        req.method = "POST"
        req.cookies = {"nexus_csrf": token}
        req.headers = {"X-CSRF-Token": token}
        require_csrf(req)  # should not raise

    def test_post_without_csrf_raises_403(self):
        from fastapi import HTTPException
        req = self._make_request("POST")
        with pytest.raises(HTTPException) as exc_info:
            require_csrf(req)
        assert exc_info.value.status_code == 403

    def test_post_with_mismatched_csrf_raises_403(self):
        from fastapi import HTTPException
        req = self._make_request("POST", cookie="aaa", header="bbb")
        with pytest.raises(HTTPException) as exc_info:
            require_csrf(req)
        assert exc_info.value.status_code == 403

    def test_delete_with_correct_csrf_passes(self):
        token = new_csrf()
        req = MagicMock()
        req.method = "DELETE"
        req.cookies = {"nexus_csrf": token}
        req.headers = {"X-CSRF-Token": token}
        require_csrf(req)


# ---------------------------------------------------------------------------
# cookie_name helper
# ---------------------------------------------------------------------------

class TestCookieName:
    def test_admin_site_returns_admin_cookie(self):
        from app.config import get_settings
        s = get_settings()
        assert cookie_name("admin") == s.admin_cookie

    def test_chat_site_returns_chat_cookie(self):
        from app.config import get_settings
        s = get_settings()
        assert cookie_name("chat") == s.chat_cookie

    def test_unknown_site_returns_chat_cookie(self):
        from app.config import get_settings
        s = get_settings()
        assert cookie_name("unknown") == s.chat_cookie


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_deterministic(self):
        k1 = cache_key("admin", "hello world", "internal")
        k2 = cache_key("admin", "hello world", "internal")
        assert k1 == k2

    def test_case_insensitive_message(self):
        k1 = cache_key("admin", "Hello World", "internal")
        k2 = cache_key("admin", "hello world", "internal")
        assert k1 == k2

    def test_different_roles_produce_different_keys(self):
        k1 = cache_key("admin", "msg", "internal")
        k2 = cache_key("internal", "msg", "internal")
        assert k1 != k2

    def test_different_visibility_produce_different_keys(self):
        k1 = cache_key("admin", "msg", "internal")
        k2 = cache_key("admin", "msg", "generic")
        assert k1 != k2

    def test_key_has_expected_prefix(self):
        k = cache_key("internal", "test", "generic")
        assert k.startswith("nexus:ans:")
