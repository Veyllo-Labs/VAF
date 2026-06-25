# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
AES-256-GCM encryption for VAF Memory System.

Provides:
- Content encryption at rest
- Key derivation from configuration
- Decrypt-on-read pattern
- Key rotation support
"""

import os
import base64
import secrets
from typing import Tuple, Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from vaf.core.config import Config


class MemoryCrypto:
    """
    AES-256-GCM encryption handler for memory content.
    
    Usage:
        crypto = MemoryCrypto()
        encrypted, nonce = crypto.encrypt("sensitive content")
        decrypted = crypto.decrypt(encrypted, nonce)
    """
    
    # AES-256 requires 32-byte key
    KEY_SIZE = 32
    # GCM nonce size (96 bits recommended)
    NONCE_SIZE = 12
    
    def __init__(self, key: Optional[bytes] = None):
        """
        Initialize crypto with encryption key.
        
        Args:
            key: 32-byte encryption key. If None, derives from config.
        """
        if key:
            self._key = key
        else:
            self._key = self._get_or_create_key()
        
        self._aesgcm = AESGCM(self._key)
    
    def _get_or_create_key(self) -> bytes:
        """
        Get encryption key from config or generate a new one.
        
        The key is stored Base64-encoded in config for portability.
        """
        encoded_key = Config.get("memory_encryption_key", "")
        
        if encoded_key:
            try:
                return base64.b64decode(encoded_key)
            except Exception:
                pass
        
        # Generate new key if none exists
        new_key = secrets.token_bytes(self.KEY_SIZE)
        Config.set("memory_encryption_key", base64.b64encode(new_key).decode())
        return new_key
    
    def encrypt(self, plaintext: str) -> Tuple[bytes, bytes]:
        """
        Encrypt plaintext content using AES-256-GCM.
        
        Args:
            plaintext: Content to encrypt (string)
            
        Returns:
            Tuple of (ciphertext, nonce) as bytes
        """
        if not plaintext:
            raise ValueError("Cannot encrypt empty content")
        
        # Generate random nonce for each encryption
        nonce = secrets.token_bytes(self.NONCE_SIZE)
        
        # Encrypt (GCM provides authentication automatically)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        
        return ciphertext, nonce
    
    def decrypt(self, ciphertext: bytes, nonce: bytes) -> str:
        """
        Decrypt ciphertext using AES-256-GCM.
        
        Args:
            ciphertext: Encrypted content
            nonce: Nonce used during encryption
            
        Returns:
            Decrypted plaintext string
            
        Raises:
            cryptography.exceptions.InvalidTag: If authentication fails
        """
        if not ciphertext or not nonce:
            raise ValueError("Ciphertext and nonce are required")
        
        plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode('utf-8')
    
    @classmethod
    def derive_key_from_password(cls, password: str, salt: Optional[bytes] = None) -> Tuple[bytes, bytes]:
        """
        Derive encryption key from password using PBKDF2.
        
        Useful for user-specific encryption or key rotation.
        
        Args:
            password: User password
            salt: Optional salt (generated if not provided)
            
        Returns:
            Tuple of (derived_key, salt)
        """
        if salt is None:
            salt = secrets.token_bytes(16)
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=cls.KEY_SIZE,
            salt=salt,
            iterations=480000,  # OWASP recommendation for 2023
        )
        
        key = kdf.derive(password.encode('utf-8'))
        return key, salt
    
    @classmethod
    def generate_key(cls) -> str:
        """
        Generate a new random encryption key.
        
        Returns:
            Base64-encoded key string (for config storage)
        """
        key = secrets.token_bytes(cls.KEY_SIZE)
        return base64.b64encode(key).decode()
    
    @classmethod
    def rotate_key(cls, old_crypto: 'MemoryCrypto', new_key: bytes) -> 'MemoryCrypto':
        """
        Create a new crypto instance for key rotation.
        
        The caller is responsible for re-encrypting all memories
        with the new key.
        
        Args:
            old_crypto: Current crypto instance (for decryption)
            new_key: New 32-byte encryption key
            
        Returns:
            New MemoryCrypto instance with the new key
        """
        if len(new_key) != cls.KEY_SIZE:
            raise ValueError(f"Key must be exactly {cls.KEY_SIZE} bytes")
        
        return cls(key=new_key)
    
    def verify_key(self) -> bool:
        """
        Verify the encryption key is valid by performing a test encrypt/decrypt.
        
        Returns:
            True if key is valid, False otherwise
        """
        try:
            test_data = "VAF Memory System Key Verification"
            ciphertext, nonce = self.encrypt(test_data)
            decrypted = self.decrypt(ciphertext, nonce)
            return decrypted == test_data
        except Exception:
            return False


# Singleton instance for the module
_crypto_instance: Optional[MemoryCrypto] = None


def get_crypto() -> MemoryCrypto:
    """
    Get the singleton MemoryCrypto instance.
    
    Returns:
        Configured MemoryCrypto instance
    """
    global _crypto_instance
    if _crypto_instance is None:
        _crypto_instance = MemoryCrypto()
    return _crypto_instance


def reset_crypto():
    """Reset the crypto instance (useful for testing or key rotation)."""
    global _crypto_instance
    _crypto_instance = None
