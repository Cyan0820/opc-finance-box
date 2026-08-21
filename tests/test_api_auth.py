from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.api_auth import ApiAuthError, hash_token, load_api_auth_policy


class ApiAuthPolicyTests(unittest.TestCase):
    def _policy(self, root: str, principals: list[dict]) -> Path:
        path = Path(root) / "api-auth.json"
        path.write_text(json.dumps({
            "schema_version": 1, "principals": principals,
        }), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_role_policy_authenticates_hashes_without_raw_tokens(self):
        reader_token = "reader-token-abcdefghijklmnopqrstuvwxyz-123456"
        admin_token = "admin-token-abcdefghijklmnopqrstuvwxyz-1234567"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._policy(temp_dir, [
                {
                    "principal_id": "finance_reader",
                    "token_sha256": hash_token(reader_token),
                    "roles": ["reader"],
                },
                {
                    "principal_id": "box_admin",
                    "token_sha256": hash_token(admin_token),
                    "roles": ["admin"],
                },
            ])
            policy = load_api_auth_policy(legacy_token="", policy_path=path)
        reader = policy.authenticate(reader_token)
        self.assertEqual(reader.principal_id, "finance_reader")
        self.assertTrue(reader.allows("reader"))
        self.assertFalse(reader.allows("operator"))
        admin = policy.authenticate(admin_token)
        self.assertTrue(all(admin.allows(role) for role in (
            "reader", "operator", "reviewer", "admin",
        )))
        self.assertIsNone(policy.authenticate("wrong-token-value-with-thirty-two-characters"))
        self.assertFalse(policy.public_dict()["raw_tokens_present"])
        self.assertNotIn(reader_token, json.dumps(policy.public_dict()))

    def test_policy_rejects_permissive_file_duplicate_hash_and_ambiguous_configuration(self):
        token_hash = hash_token("valid-token-abcdefghijklmnopqrstuvwxyz-123456")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._policy(temp_dir, [{
                "principal_id": "reader", "token_sha256": token_hash, "roles": ["reader"],
            }])
            path.chmod(0o644)
            with self.assertRaisesRegex(ApiAuthError, "permissions"):
                load_api_auth_policy(legacy_token="", policy_path=path)
            path = self._policy(temp_dir, [
                {"principal_id": "a", "token_sha256": token_hash, "roles": ["reader"]},
                {"principal_id": "b", "token_sha256": token_hash, "roles": ["reviewer"]},
            ])
            with self.assertRaisesRegex(ApiAuthError, "same token hash"):
                load_api_auth_policy(legacy_token="", policy_path=path)
            with self.assertRaisesRegex(ApiAuthError, "either"):
                load_api_auth_policy(legacy_token="x" * 32, policy_path=path)
            raw_token_path = self._policy(temp_dir, [{
                "principal_id": "unsafe", "token_sha256": token_hash,
                "roles": ["reader"], "token": "must-not-be-stored",
            }])
            with self.assertRaisesRegex(ApiAuthError, "only principal_id"):
                load_api_auth_policy(legacy_token="", policy_path=raw_token_path)

    def test_token_hash_requires_high_entropy_transport_shape(self):
        with self.assertRaisesRegex(ApiAuthError, "at least 32"):
            hash_token("short")
        with self.assertRaisesRegex(ApiAuthError, "non-space ASCII"):
            hash_token("x" * 31 + " ")
        self.assertRegex(hash_token("x" * 32), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
