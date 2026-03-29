"""Dead Drop encryption — Fernet symmetric encryption with passphrase-derived keys."""

import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

# Use Fernet if cryptography is available, fall back to simple XOR obfuscation
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning(
        "cryptography library not installed — Dead Drop will use basic obfuscation. "
        "Install for real encryption: pip install cryptography"
    )

# Fixed salt for key derivation (consistent across nodes)
_SALT = b"loracle-dead-drop-v1"


def derive_key(passphrase: str) -> bytes:
    """Derive a Fernet key from a passphrase using PBKDF2.

    Args:
        passphrase: User-provided passphrase string.

    Returns:
        32-byte URL-safe base64-encoded key suitable for Fernet.
    """
    if CRYPTO_AVAILABLE:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_SALT,
            iterations=100_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
        return key
    else:
        # Fallback: SHA-256 hash as key
        raw = hashlib.sha256(_SALT + passphrase.encode()).digest()
        return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str, key: bytes) -> bytes:
    """Encrypt a message string.

    Args:
        plaintext: Message to encrypt.
        key: Key from derive_key().

    Returns:
        Encrypted bytes.
    """
    if CRYPTO_AVAILABLE:
        f = Fernet(key)
        return f.encrypt(plaintext.encode("utf-8"))
    else:
        # XOR obfuscation fallback
        key_bytes = base64.urlsafe_b64decode(key)
        data = plaintext.encode("utf-8")
        encrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data))
        return base64.urlsafe_b64encode(encrypted)


def decrypt(ciphertext: bytes, key: bytes) -> str:
    """Decrypt a message.

    Args:
        ciphertext: Encrypted bytes from encrypt().
        key: Key from derive_key().

    Returns:
        Decrypted plaintext string.

    Raises:
        Exception: If decryption fails (wrong key, corrupted data).
    """
    if CRYPTO_AVAILABLE:
        f = Fernet(key)
        return f.decrypt(ciphertext).decode("utf-8")
    else:
        key_bytes = base64.urlsafe_b64decode(key)
        data = base64.urlsafe_b64decode(ciphertext)
        decrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data))
        return decrypted.decode("utf-8")
