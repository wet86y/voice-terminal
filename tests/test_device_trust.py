from __future__ import annotations

import base64
import struct
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from voice_terminal.devices.trust import (
    DevicePairingManager,
    DeviceRequestAuthenticator,
    DeviceTrustError,
    DeviceTrustStore,
    device_request_proof_message,
    pairing_proof_message,
    parse_ed25519_public_key,
)


def ssh_public_key(private_key: Ed25519PrivateKey) -> str:
    """OpenSSH 公钥编码：构造实际的 ssh-ed25519 wire 格式。"""
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    algorithm = b"ssh-ed25519"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", len(raw)) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} workstation"


class DeviceTrustStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_store_is_empty_and_approval_requires_ed25519_signature(self) -> None:
        store = DeviceTrustStore(self.root)
        self.assertEqual(store.state, "missing")
        self.assertEqual(store.snapshot()["devices"], [])

        private_key = Ed25519PrivateKey.generate()
        public_key = ssh_public_key(private_key)
        record = store.approve("desktop-a", public_key, trusted_at_ms=100)
        message = b"voice-terminal-device-channel-v1\x00desktop-a\x00nonce"
        signature = private_key.sign(message)

        self.assertTrue(store.authorize("desktop-a", public_key, message, signature))
        self.assertFalse(store.authorize("desktop-b", public_key, message, signature))
        self.assertFalse(store.authorize("desktop-a", public_key, message + b"x", signature))
        self.assertEqual(store.trusted_key("desktop-a"), record.public_key)
        self.assertEqual(record.fingerprint, store.snapshot()["devices"][0]["fingerprint"])

    def test_revoke_persists_and_a_reapproval_is_explicit(self) -> None:
        key = Ed25519PrivateKey.generate()
        public_key = ssh_public_key(key)
        store = DeviceTrustStore(self.root)
        store.approve("desktop-a", public_key, trusted_at_ms=100)

        self.assertTrue(store.revoke("desktop-a", revoked_at_ms=200))
        self.assertIsNone(store.trusted_key("desktop-a"))
        reloaded = DeviceTrustStore(self.root)
        self.assertEqual(reloaded.state, "valid")
        self.assertEqual(reloaded.snapshot()["devices"][0]["state"], "revoked")

        reloaded.approve("desktop-a", public_key, trusted_at_ms=300)
        self.assertEqual(reloaded.trusted_key("desktop-a"), parse_ed25519_public_key(public_key)[0])
        self.assertEqual(reloaded.snapshot()["devices"][0]["state"], "trusted")

    def test_replacing_a_key_requires_revoke_then_explicit_approval(self) -> None:
        key_a = ssh_public_key(Ed25519PrivateKey.generate())
        key_b = ssh_public_key(Ed25519PrivateKey.generate())
        store = DeviceTrustStore(self.root)
        store.approve("desktop-a", key_a, trusted_at_ms=100)

        with self.assertRaises(DeviceTrustError):
            store.approve("desktop-a", key_b, trusted_at_ms=150)

        self.assertTrue(store.revoke("desktop-a", revoked_at_ms=200))
        store.approve("desktop-a", key_b, trusted_at_ms=300)
        self.assertEqual(store.trusted_key("desktop-a"), parse_ed25519_public_key(key_b)[0])
        self.assertEqual(store.snapshot()["devices"][0]["state"], "trusted")

    def test_same_key_cannot_be_bound_to_two_device_ids_and_other_key_requires_revoke(self) -> None:
        key_a = ssh_public_key(Ed25519PrivateKey.generate())
        key_b = ssh_public_key(Ed25519PrivateKey.generate())
        store = DeviceTrustStore(self.root)
        store.approve("desktop-a", key_a, trusted_at_ms=10)
        with self.assertRaises(DeviceTrustError):
            store.approve("desktop-b", key_a, trusted_at_ms=11)
        with self.assertRaises(DeviceTrustError):
            store.approve("desktop-a", key_b, trusted_at_ms=12)
        self.assertEqual(store.trusted_key("desktop-a"), parse_ed25519_public_key(key_a)[0])
        self.assertIsNone(store.trusted_key("desktop-b"))

    def test_invalid_store_is_preserved_and_blocks_write(self) -> None:
        state = self.root / "state"
        state.mkdir()
        path = state / "device-trust.json"
        original = b'{"schema_version":91,"private":"preserve"}'
        path.write_bytes(original)
        store = DeviceTrustStore(self.root)
        self.assertEqual(store.state, "invalid")
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(DeviceTrustError):
            store.approve("desktop-a", ssh_public_key(Ed25519PrivateKey.generate()))
        self.assertEqual(path.read_bytes(), original)

    def test_store_rechecks_disk_before_mutation(self) -> None:
        store = DeviceTrustStore(self.root)
        path = self.root / "state" / "device-trust.json"
        path.parent.mkdir()
        original = b'{"schema_version":99,"devices":[]}'
        path.write_bytes(original)
        with self.assertRaises(DeviceTrustError):
            store.approve("desktop-a", ssh_public_key(Ed25519PrivateKey.generate()))
        self.assertEqual(path.read_bytes(), original)

    def test_production_read_only_view_refreshes_native_changes_and_cannot_write(self) -> None:
        writer = DeviceTrustStore(self.root)
        reader = DeviceTrustStore(self.root, read_only=True)
        key = ssh_public_key(Ed25519PrivateKey.generate())
        writer.approve("desktop-a", key, trusted_at_ms=100)
        self.assertEqual(reader.trusted_key("desktop-a"), parse_ed25519_public_key(key)[0])
        self.assertEqual(reader.snapshot()["state"], "valid")
        with self.assertRaises(DeviceTrustError):
            reader.approve("desktop-b", ssh_public_key(Ed25519PrivateKey.generate()))
        writer.revoke("desktop-a", revoked_at_ms=200)
        self.assertIsNone(reader.trusted_key("desktop-a"))
        self.assertEqual(reader.snapshot()["devices"][0]["state"], "revoked")

    def test_read_only_view_revokes_cached_key_after_external_store_removal_or_corruption(self) -> None:
        for failure in ("removed", "corrupted"):
            with self.subTest(failure=failure):
                root = self.root / failure
                writer = DeviceTrustStore(root)
                reader = DeviceTrustStore(root, read_only=True)
                private_key = Ed25519PrivateKey.generate()
                public_key = ssh_public_key(private_key)
                writer.approve("desktop-a", public_key, trusted_at_ms=100)
                message = b"voice-terminal-device-channel-v1\x00desktop-a\x00nonce"
                signature = private_key.sign(message)
                path = root / "state" / "device-trust.json"
                original = path.read_bytes()

                self.assertTrue(reader.verify_signature("desktop-a", message, signature))
                if failure == "removed":
                    path.unlink()
                else:
                    path.write_bytes(b'{"schema_version":1,"devices":[]} trailing')

                self.assertFalse(reader.verify_signature("desktop-a", message, signature))
                self.assertIsNone(reader.trusted_key("desktop-a"))
                self.assertEqual(reader.state, "invalid")
                self.assertEqual(reader.snapshot()["devices"], [])

                # 原生层修复并恢复原内容后，必须重新严格解析，不可沿用空的失效快照。
                path.write_bytes(original)
                self.assertTrue(reader.verify_signature("desktop-a", message, signature))
                self.assertEqual(reader.state, "valid")

    def test_malformed_public_keys_and_duplicate_json_fields_are_rejected(self) -> None:
        for key in ("ssh-rsa AAAA", "ssh-ed25519 invalid-base64", "ssh-ed25519 AAAA bad"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_ed25519_public_key(key)

        state = self.root / "state"
        state.mkdir()
        path = state / "device-trust.json"
        path.write_text('{"schema_version":1,"schema_version":1,"devices":[],"fields":{}}', encoding="utf-8")
        store = DeviceTrustStore(self.root)
        self.assertEqual(store.state, "invalid")
        self.assertIn("重复字段", store.error or "")


class DevicePairingManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DeviceTrustStore(self.root)
        self.manager = DevicePairingManager(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def submit(manager: DevicePairingManager, private_key: Ed25519PrivateKey, device_id: str,
               code: str, *, nonce: bytes = b"n" * 24, now_ms: int = 10_000):
        public_key = ssh_public_key(private_key)
        _raw_key, fingerprint = parse_ed25519_public_key(public_key)
        signature = private_key.sign(pairing_proof_message(device_id, fingerprint, code, nonce))
        return manager.submit_request(device_id, public_key, code, nonce, signature, now_ms=now_ms)

    def test_approval_is_explicit_and_only_public_identity_is_persisted(self) -> None:
        code, expires_at = self.manager.open_window(ttl_seconds=30, now_ms=10_000)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isascii() and code.isdigit())
        self.assertEqual(expires_at, 40_000)
        private_key = Ed25519PrivateKey.generate()
        pending = self.submit(self.manager, private_key, "desktop-a", code)

        self.assertIsNone(self.store.trusted_key("desktop-a"))
        pending_snapshot = self.manager.pending_requests(now_ms=11_000)
        self.assertEqual(pending_snapshot[0]["request_id"], pending.request_id)
        self.assertNotIn(code, repr(pending_snapshot))
        self.assertNotIn("public_key", pending_snapshot[0])

        trusted = self.manager.approve(pending.request_id, now_ms=12_000)
        self.assertEqual(trusted.device_id, "desktop-a")
        self.assertEqual(self.store.trusted_key("desktop-a"), trusted.public_key)
        serialized = (self.root / "state" / "device-trust.json").read_text(encoding="utf-8")
        self.assertNotIn(code, serialized)
        self.assertNotIn("private", serialized)

    def test_product_trust_view_rejects_python_pairing_window_creation(self) -> None:
        store = DeviceTrustStore(self.root, read_only=True)
        manager = DevicePairingManager(store)
        with self.assertRaises(DeviceTrustError):
            manager.open_window(now_ms=10_000)

    def test_wrong_code_and_invalid_signature_are_rejected(self) -> None:
        code, _expires_at = self.manager.open_window(now_ms=10_000)
        private_key = Ed25519PrivateKey.generate()
        wrong_code = "999999" if code != "999999" else "888888"
        with self.assertRaises(DeviceTrustError):
            self.submit(self.manager, private_key, "desktop-a", wrong_code)
        self.assertEqual(self.manager.status(now_ms=10_000)["failed_attempts_remaining"], 9)

        public_key = ssh_public_key(private_key)
        _raw_key, fingerprint = parse_ed25519_public_key(public_key)
        proof = pairing_proof_message("desktop-a", fingerprint, code, b"x" * 24)
        with self.assertRaises(DeviceTrustError):
            self.manager.submit_request("desktop-a", public_key, code, b"x" * 24,
                                        private_key.sign(proof + b"tampered"), now_ms=10_000)
        self.assertEqual(self.manager.status(now_ms=10_000)["failed_attempts_remaining"], 9)
        self.assertEqual(self.manager.pending_requests(now_ms=10_000), [])

    def test_pairing_window_closes_after_ten_signed_wrong_code_attempts(self) -> None:
        code, _expires_at = self.manager.open_window(now_ms=10_000)
        wrong_code = "999999" if code != "999999" else "888888"
        private_key = Ed25519PrivateKey.generate()
        for index in range(DevicePairingManager.MAX_INVALID_CODE_ATTEMPTS):
            with self.subTest(index=index), self.assertRaises(DeviceTrustError):
                self.submit(self.manager, private_key, "desktop-a", wrong_code,
                            nonce=bytes([index + 1]) * 24, now_ms=10_000 + index)
        status = self.manager.status(now_ms=10_010)
        self.assertFalse(status["window_open"])
        self.assertEqual(status["failed_attempts_remaining"], 0)
        self.assertIsNone(self.store.trusted_key("desktop-a"))

    def test_window_close_expires_code_and_pending_request(self) -> None:
        code, _expires_at = self.manager.open_window(now_ms=10_000)
        pending = self.submit(self.manager, Ed25519PrivateKey.generate(), "desktop-a", code)
        self.manager.close_window()
        self.assertEqual(self.manager.pending_requests(now_ms=10_000), [])
        self.assertIsNone(self.store.trusted_key("desktop-a"))
        with self.assertRaises(DeviceTrustError):
            self.manager.approve(pending.request_id, now_ms=10_000)

    def test_pairing_window_and_pending_request_have_bounded_lifetimes(self) -> None:
        with self.assertRaises(DeviceTrustError):
            self.manager.open_window(ttl_seconds=29, now_ms=1_000)
        code, _expires_at = self.manager.open_window(ttl_seconds=600, now_ms=1_000)
        pending = self.submit(self.manager, Ed25519PrivateKey.generate(), "desktop-a", code, now_ms=1_000)
        self.assertEqual(self.manager.pending_requests(now_ms=pending.expires_at_ms), [])
        with self.assertRaises(DeviceTrustError):
            self.manager.approve(pending.request_id, now_ms=pending.expires_at_ms)

    def test_duplicate_request_is_idempotent_but_conflicting_device_is_rejected(self) -> None:
        code, _expires_at = self.manager.open_window(now_ms=10_000)
        private_key = Ed25519PrivateKey.generate()
        first = self.submit(self.manager, private_key, "desktop-a", code, nonce=b"a" * 24)
        retry = self.submit(self.manager, private_key, "desktop-a", code, nonce=b"a" * 24)
        self.assertEqual(first, retry)
        with self.assertRaises(DeviceTrustError):
            self.submit(self.manager, private_key, "desktop-a", code, nonce=b"b" * 24)

    def test_invalid_trust_store_cannot_be_approved(self) -> None:
        path = self.root / "state" / "device-trust.json"
        path.parent.mkdir()
        path.write_text('{"schema_version":99,"devices":[]}', encoding="utf-8")
        invalid_store = DeviceTrustStore(self.root)
        manager = DevicePairingManager(invalid_store)
        code, _expires_at = manager.open_window(now_ms=10_000)
        pending = self.submit(manager, Ed25519PrivateKey.generate(), "desktop-a", code)
        with self.assertRaises(DeviceTrustError):
            manager.approve(pending.request_id, now_ms=11_000)
        self.assertIsNone(invalid_store.trusted_key("desktop-a"))
        self.assertTrue(manager.pending_requests(now_ms=11_000))
        self.assertEqual(path.read_text(encoding="utf-8"), '{"schema_version":99,"devices":[]}')


class DeviceRequestAuthenticatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DeviceTrustStore(self.root)
        self.private_key = Ed25519PrivateKey.generate()
        self.store.approve("desktop-a", ssh_public_key(self.private_key), trusted_at_ms=10_000)
        self.auth = DeviceRequestAuthenticator(self.store, core_session_id="1" * 32)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def signed_request(self, *, path="/v1/status", payload=None, timestamp_ms=10_000,
                       now_monotonic=5.0, device_id="desktop-a", core_session_id=None):
        payload = payload or {"protocol_version": 1}
        core_id = core_session_id or self.auth.core_session_id
        challenge = self.auth.issue_challenge(
            device_id, "POST", path, now_ms=timestamp_ms, now_monotonic=now_monotonic,
        )["challenge"]
        proof = device_request_proof_message(
            method="POST", path=path, device_id=device_id, core_session_id=core_id,
            challenge=challenge, timestamp_ms=timestamp_ms, payload=payload,
        )
        return {
            "method": "POST", "path": path, "device_id": device_id,
            "core_session_id": core_id, "challenge": challenge,
            "timestamp_ms": timestamp_ms, "signature": self.private_key.sign(proof),
            "payload": payload,
        }

    def test_business_signature_is_bound_to_request_body_and_consumed_challenge(self) -> None:
        request = self.signed_request(payload={"protocol_version": 1})
        self.assertTrue(self.auth.authorize_request(**request, now_ms=10_001, now_monotonic=5.1))
        self.assertFalse(self.auth.authorize_request(**request, now_ms=10_001, now_monotonic=5.2))

        altered = self.signed_request(payload={"protocol_version": 1})
        altered["payload"] = {"protocol_version": 1, "extra": True}
        self.assertFalse(self.auth.authorize_request(**altered, now_ms=10_001, now_monotonic=5.2))

    def test_challenge_expiry_core_generation_timestamp_and_revocation_reject(self) -> None:
        expired = self.signed_request()
        self.assertFalse(self.auth.authorize_request(**expired, now_ms=10_001, now_monotonic=36.0))

        wrong_session = self.signed_request()
        wrong_session["core_session_id"] = "2" * 32
        self.assertFalse(self.auth.authorize_request(**wrong_session, now_ms=10_001, now_monotonic=6.0))

        stale = self.signed_request(timestamp_ms=10_000)
        self.assertFalse(self.auth.authorize_request(**stale, now_ms=10_000 + self.auth.TIMESTAMP_TOLERANCE_MS + 1,
                                                     now_monotonic=6.0))

        revoked = self.signed_request(now_monotonic=7.0)
        self.store.revoke("desktop-a", revoked_at_ms=11_000)
        self.assertFalse(self.auth.authorize_request(**revoked, now_ms=10_001, now_monotonic=7.1))

    def test_challenge_cannot_be_issued_for_unlisted_or_wrong_media_method(self) -> None:
        for method, path in (("GET", "/v1/status"), ("POST", "/api/v1/settings"),
                             ("POST", "/v1/media"), ("POST", "/v1/file-transfer.extra")):
            with self.subTest(method=method, path=path), self.assertRaises(DeviceTrustError):
                self.auth.issue_challenge("desktop-a", method, path, now_ms=10_000, now_monotonic=1.0)

    def test_file_transfer_challenge_is_fixed_to_the_exact_post_route(self) -> None:
        challenge = self.auth.issue_challenge(
            "desktop-a", "POST", "/v1/file-transfer", now_ms=10_000, now_monotonic=1.0,
        )
        self.assertEqual(challenge["protocol_version"], 1)
        self.assertTrue(self.auth.authorize_request(
            method="POST", path="/v1/file-transfer", device_id="desktop-a",
            core_session_id=self.auth.core_session_id, challenge=challenge["challenge"],
            timestamp_ms=10_000, signature=self.private_key.sign(device_request_proof_message(
                method="POST", path="/v1/file-transfer", device_id="desktop-a",
                core_session_id=self.auth.core_session_id, challenge=challenge["challenge"],
                timestamp_ms=10_000, payload={"protocol_version": 1, "action": "begin"},
            )), payload={"protocol_version": 1, "action": "begin"},
            now_ms=10_001, now_monotonic=1.1,
        ))

    def test_media_upgrade_signature_is_bound_to_get_path_and_handshake_payload(self) -> None:
        payload = {
            "protocol_version": 1,
            "stream_id": "stream-a",
            "connection_generation": 2,
            "executor_generation": 3,
            "interaction_generation": 4,
        }
        challenge = self.auth.issue_challenge(
            "desktop-a", "GET", "/v1/media", now_ms=10_000, now_monotonic=1.0,
        )["challenge"]
        proof = device_request_proof_message(
            method="GET", path="/v1/media", device_id="desktop-a",
            core_session_id=self.auth.core_session_id, challenge=challenge,
            timestamp_ms=10_000, payload=payload,
        )
        request = {
            "method": "GET", "path": "/v1/media", "device_id": "desktop-a",
            "core_session_id": self.auth.core_session_id, "challenge": challenge,
            "timestamp_ms": 10_000, "signature": self.private_key.sign(proof), "payload": payload,
        }
        self.assertTrue(self.auth.authorize_request(**request, now_ms=10_001, now_monotonic=1.1))
        self.assertFalse(self.auth.authorize_request(**request, now_ms=10_001, now_monotonic=1.2))

    def test_device_request_proof_matches_the_rust_client_vector(self) -> None:
        message = device_request_proof_message(
            method="POST", path="/v1/devices", device_id="client-a",
            core_session_id="0123456789abcdef0123456789abcdef", challenge="a" * 43,
            timestamp_ms=1_700_000_000_123, payload={"protocol_version": 1},
        )
        self.assertEqual(
            message.hex(),
            "766f6963652d7465726d696e616c2d6465766963652d726571756573742d76310000000004504f53540000000b2f76312f6465766963657300000008636c69656e742d610000002030313233343536373839616263646566303132333435363738396162636465660000002b616161616161616161616161616161616161616161616161616161616161616161616161616161616161610000000d313730303030303030303132330000002000aa4c2c857995eb8e19cb0fade07e4b49aac774e37a99c37a0c9f549204d9de",
        )


if __name__ == "__main__":
    unittest.main()
