"""Encryption codec for securing LangGraph state in Temporal event history.

Provides `EncryptionCodec` implementing Temporal's `PayloadCodec` protocol
for encrypting/decrypting payloads using AES-GCM or Fernet. This ensures
that PII and sensitive data in channel state is encrypted at rest in
Temporal's event history.

Usage:
    ```python
    from langgraph.temporal.encryption import EncryptionCodec

    codec = EncryptionCodec(key=b"your-32-byte-key-here-for-aes!!")
    client = await Client.connect(
        "localhost:7233",
        data_converter=DataConverter(payload_codec=codec),
    )
    ```
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec


class EncryptionCodec(PayloadCodec):
    """Temporal PayloadCodec that encrypts/decrypts payloads using AES-GCM.

    All data flowing through Temporal's event history (Activity inputs/outputs,
    Workflow state, Signal payloads) will be encrypted with the provided key.

    This is critical for workloads containing PII, as Temporal's event history
    is queryable and exportable. Without encryption, all channel state
    (including `messages` lists containing user data) is stored in plaintext.

    Args:
        key: 32-byte encryption key for AES-256-GCM. If not provided,
            falls back to the `LANGGRAPH_TEMPORAL_ENCRYPTION_KEY` environment
            variable (hex-encoded).

    Raises:
        ValueError: If no key is provided and the environment variable is not set.
        ValueError: If the key is not exactly 32 bytes.
    """

    ENCODING = "binary/encrypted"
    METADATA_KEY = "encryption-key-id"

    def __init__(
        self,
        key: bytes | None = None,
        *,
        key_id: str = "default",
    ) -> None:
        env_key = os.environ.get("LANGGRAPH_TEMPORAL_ENCRYPTION_KEY")
        if key is not None:
            self._key = key
        elif env_key is not None:
            self._key = bytes.fromhex(env_key)
        else:
            raise ValueError(
                "Encryption key required. Provide `key` parameter or set "
                "LANGGRAPH_TEMPORAL_ENCRYPTION_KEY environment variable "
                "(hex-encoded 32-byte key)."
            )

        if len(self._key) != 32:
            raise ValueError(
                f"Encryption key must be exactly 32 bytes (got {len(self._key)}). "
                "Use `os.urandom(32)` to generate a suitable key."
            )

        self._key_id = key_id

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Encrypt payloads before they enter Temporal's event history."""
        return [self._encrypt_payload(p) for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Decrypt payloads retrieved from Temporal's event history."""
        return [self._decrypt_payload(p) for p in payloads]

    def _encrypt_payload(self, payload: Payload) -> Payload:
        """Encrypt a single payload using AES-256-GCM."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import (  # type: ignore[import-not-found]
                AESGCM,
            )
        except ImportError:
            raise ImportError(
                "The `cryptography` package is required for encryption. "
                "Install it with: pip install cryptography"
            ) from None

        nonce = os.urandom(12)
        aesgcm = AESGCM(self._key)

        # Encrypt the serialized payload data
        plaintext = payload.SerializeToString()
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        return Payload(
            metadata={
                "encoding": self.ENCODING.encode(),
                self.METADATA_KEY: self._key_id.encode(),
            },
            data=nonce + ciphertext,
        )

    def _decrypt_payload(self, payload: Payload) -> Payload:
        """Decrypt a single payload using AES-256-GCM."""
        encoding = payload.metadata.get("encoding", b"").decode()
        if encoding != self.ENCODING:
            return payload

        try:
            from cryptography.hazmat.primitives.ciphers.aead import (
                AESGCM,
            )
        except ImportError:
            raise ImportError(
                "The `cryptography` package is required for decryption. "
                "Install it with: pip install cryptography"
            ) from None

        nonce = payload.data[:12]
        ciphertext = payload.data[12:]

        aesgcm = AESGCM(self._key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        result = Payload()
        result.ParseFromString(plaintext)
        return result


class FernetEncryptionCodec(PayloadCodec):
    """Temporal PayloadCodec using Fernet symmetric encryption.

    Fernet is simpler to use than AES-GCM (key is a URL-safe base64 string)
    and provides authenticated encryption. Use this for simpler deployments
    where AES-GCM key management is not needed.

    Args:
        key: Fernet key (URL-safe base64-encoded 32-byte key).
            Generate with `Fernet.generate_key()`.
    """

    ENCODING = "binary/fernet-encrypted"

    def __init__(self, key: str | bytes) -> None:
        self._key = key if isinstance(key, bytes) else key.encode()

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Encrypt payloads using Fernet."""
        return [self._encrypt_payload(p) for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Decrypt payloads using Fernet."""
        return [self._decrypt_payload(p) for p in payloads]

    def _encrypt_payload(self, payload: Payload) -> Payload:
        """Encrypt a single payload using Fernet."""
        try:
            from cryptography.fernet import Fernet  # type: ignore[import-not-found]
        except ImportError:
            raise ImportError(
                "The `cryptography` package is required for Fernet encryption. "
                "Install it with: pip install cryptography"
            ) from None

        f = Fernet(self._key)
        plaintext = payload.SerializeToString()
        ciphertext = f.encrypt(plaintext)

        return Payload(
            metadata={"encoding": self.ENCODING.encode()},
            data=ciphertext,
        )

    def _decrypt_payload(self, payload: Payload) -> Payload:
        """Decrypt a single payload using Fernet."""
        encoding = payload.metadata.get("encoding", b"").decode()
        if encoding != self.ENCODING:
            return payload

        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise ImportError(
                "The `cryptography` package is required for Fernet decryption. "
                "Install it with: pip install cryptography"
            ) from None

        f = Fernet(self._key)
        plaintext = f.decrypt(payload.data)

        result = Payload()
        result.ParseFromString(plaintext)
        return result


def generate_encryption_key() -> bytes:
    """Generate a random 32-byte encryption key for AES-256-GCM.

    Returns:
        A 32-byte random key suitable for use with `EncryptionCodec`.
    """
    return os.urandom(32)
