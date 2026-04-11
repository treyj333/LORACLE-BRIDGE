"""Tests for Dead Drop crypto module (Phase 5)."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from addons.dead_drop.crypto import derive_key, encrypt, decrypt, CRYPTO_AVAILABLE


class TestDeriveKey(unittest.TestCase):
    def test_deterministic(self):
        """Same passphrase always produces same key."""
        k1 = derive_key("test-passphrase")
        k2 = derive_key("test-passphrase")
        self.assertEqual(k1, k2)

    def test_different_passphrases_differ(self):
        k1 = derive_key("alpha")
        k2 = derive_key("bravo")
        self.assertNotEqual(k1, k2)

    def test_returns_bytes(self):
        self.assertIsInstance(derive_key("test"), bytes)


class TestEncryptDecrypt(unittest.TestCase):
    def setUp(self):
        self.key = derive_key("my-secret")

    def test_round_trip(self):
        """Encrypt then decrypt should yield original."""
        plaintext = "Hello, mesh network!"
        ciphertext = encrypt(plaintext, self.key)
        decrypted = decrypt(ciphertext, self.key)
        self.assertEqual(decrypted, plaintext)

    def test_unicode_round_trip(self):
        plaintext = "Unicode test: \u2603 \u2764 \u00e9\u00f1\u00fc"
        ciphertext = encrypt(plaintext, self.key)
        decrypted = decrypt(ciphertext, self.key)
        self.assertEqual(decrypted, plaintext)

    def test_empty_string_round_trip(self):
        ciphertext = encrypt("", self.key)
        decrypted = decrypt(ciphertext, self.key)
        self.assertEqual(decrypted, "")

    def test_wrong_key_fails(self):
        ciphertext = encrypt("secret message", self.key)
        wrong_key = derive_key("wrong-passphrase")
        with self.assertRaises(Exception):
            decrypt(ciphertext, wrong_key)

    def test_ciphertext_is_bytes(self):
        ciphertext = encrypt("test", self.key)
        self.assertIsInstance(ciphertext, bytes)

    def test_corrupted_ciphertext_fails(self):
        ciphertext = encrypt("test", self.key)
        corrupted = ciphertext[:-5] + b"XXXXX"
        with self.assertRaises(Exception):
            decrypt(corrupted, self.key)


if __name__ == "__main__":
    unittest.main()
