"""Tests for password hashing migration (Argon2id + legacy verification).

Covers:
  * hash_password now produces Argon2id PHC strings and stays API-compatible.
  * verify_password accepts Argon2id and the legacy SHA256 formats.
  * verify_password_ex reports validity, upgrade need, and a replacement hash.
  * legacy formats: both "sha256$salt$digest" and bare "salt$digest".
  * outdated Argon2 params are flagged for rehash.
  * constant-time comparison is used for legacy verification.
  * unicode passwords, wrong passwords, and malformed inputs.
"""
import hashlib
import unittest

from argon2 import PasswordHasher
from argon2.low_level import Type

from server.app import util


def _legacy_prefixed(pw: str, salt: str = "deadbeef") -> str:
    digest = hashlib.sha256((salt + pw).encode()).hexdigest()
    return f"sha256${salt}${digest}"


def _legacy_bare(pw: str, salt: str = "deadbeef") -> str:
    digest = hashlib.sha256((salt + pw).encode()).hexdigest()
    return f"{salt}${digest}"


class HashPasswordTests(unittest.TestCase):
    def test_produces_argon2id(self):
        h = util.hash_password("hunter2")
        self.assertTrue(h.startswith("$argon2id$"))

    def test_api_compatible_string_return(self):
        h = util.hash_password("hunter2")
        self.assertIsInstance(h, str)

    def test_salted_unique_hashes(self):
        a = util.hash_password("same")
        b = util.hash_password("same")
        self.assertNotEqual(a, b)

    def test_roundtrip_verify(self):
        h = util.hash_password("p@ssw0rd!")
        self.assertTrue(util.verify_password("p@ssw0rd!", h))

    def test_wrong_password_fails(self):
        h = util.hash_password("correct")
        self.assertFalse(util.verify_password("incorrect", h))

    def test_unicode_password(self):
        pw = "pä$$wörd–🔐"
        h = util.hash_password(pw)
        self.assertTrue(util.verify_password(pw, h))
        self.assertFalse(util.verify_password(pw + "x", h))


class VerifyExArgon2Tests(unittest.TestCase):
    def test_valid_current_no_upgrade(self):
        h = util.hash_password("secret")
        res = util.verify_password_ex("secret", h)
        self.assertTrue(res.valid)
        self.assertFalse(res.needs_upgrade)
        self.assertIsNone(res.replacement)
        self.assertTrue(bool(res))

    def test_invalid_argon2(self):
        h = util.hash_password("secret")
        res = util.verify_password_ex("nope", h)
        self.assertFalse(res.valid)
        self.assertFalse(res.needs_upgrade)
        self.assertIsNone(res.replacement)
        self.assertFalse(bool(res))

    def test_weak_argon2_flagged_for_upgrade(self):
        weak = PasswordHasher(
            time_cost=1, memory_cost=8, parallelism=1,
            hash_len=16, salt_len=8, type=Type.ID,
        )
        stored = weak.hash("secret")
        res = util.verify_password_ex("secret", stored)
        self.assertTrue(res.valid)
        self.assertTrue(res.needs_upgrade)
        self.assertIsNotNone(res.replacement)
        self.assertTrue(res.replacement.startswith("$argon2id$"))
        # Replacement must verify and must itself be up-to-date.
        upgraded = util.verify_password_ex("secret", res.replacement)
        self.assertTrue(upgraded.valid)
        self.assertFalse(upgraded.needs_upgrade)


class LegacyVerificationTests(unittest.TestCase):
    def test_legacy_prefixed_valid(self):
        stored = _legacy_prefixed("letmein")
        res = util.verify_password_ex("letmein", stored)
        self.assertTrue(res.valid)
        self.assertTrue(res.needs_upgrade)
        self.assertIsNotNone(res.replacement)
        self.assertTrue(res.replacement.startswith("$argon2id$"))

    def test_legacy_bare_valid(self):
        stored = _legacy_bare("letmein")
        res = util.verify_password_ex("letmein", stored)
        self.assertTrue(res.valid)
        self.assertTrue(res.needs_upgrade)

    def test_legacy_replacement_verifies(self):
        stored = _legacy_prefixed("topsecret", salt="cafebabe")
        res = util.verify_password_ex("topsecret", stored)
        self.assertTrue(util.verify_password("topsecret", res.replacement))

    def test_legacy_wrong_password(self):
        stored = _legacy_prefixed("letmein")
        res = util.verify_password_ex("wrong", stored)
        self.assertFalse(res.valid)
        self.assertFalse(res.needs_upgrade)
        self.assertIsNone(res.replacement)

    def test_verify_password_boolean_legacy(self):
        self.assertTrue(util.verify_password("x", _legacy_bare("x")))
        self.assertTrue(util.verify_password("x", _legacy_prefixed("x")))
        self.assertFalse(util.verify_password("y", _legacy_bare("x")))


class MalformedInputTests(unittest.TestCase):
    def test_empty_stored(self):
        res = util.verify_password_ex("pw", "")
        self.assertFalse(res.valid)

    def test_no_separator(self):
        self.assertFalse(util.verify_password("pw", "notahash"))

    def test_bad_digest_length(self):
        # digest that is not 64 hex chars should not be treated as legacy.
        self.assertFalse(util.verify_password("pw", "salt$abcdef"))

    def test_non_hex_digest(self):
        bad = "salt$" + ("z" * 64)
        self.assertFalse(util.verify_password("pw", bad))

    def test_non_string_stored(self):
        res = util.verify_password_ex("pw", None)  # type: ignore[arg-type]
        self.assertFalse(res.valid)


class ConstantTimeTests(unittest.TestCase):
    def test_legacy_uses_compare_digest(self):
        import server.app.util as u
        calls = {"n": 0}
        real = u.hmac.compare_digest

        def spy(a, b):
            calls["n"] += 1
            return real(a, b)

        u.hmac.compare_digest = spy
        try:
            u.verify_password("secret", _legacy_bare("secret"))
        finally:
            u.hmac.compare_digest = real
        self.assertGreaterEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
