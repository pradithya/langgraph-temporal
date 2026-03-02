"""Tests for the EncryptionCodec and FernetEncryptionCodec."""

from __future__ import annotations

import os

import pytest

from langgraph.temporal.encryption import EncryptionCodec, generate_encryption_key


class TestGenerateEncryptionKey:
    def test_generates_32_bytes(self) -> None:
        key = generate_encryption_key()
        assert len(key) == 32
        assert isinstance(key, bytes)

    def test_generates_unique_keys(self) -> None:
        key1 = generate_encryption_key()
        key2 = generate_encryption_key()
        assert key1 != key2


class TestEncryptionCodecInit:
    def test_with_valid_key(self) -> None:
        key = os.urandom(32)
        codec = EncryptionCodec(key=key)
        assert codec._key == key

    def test_with_invalid_key_length(self) -> None:
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            EncryptionCodec(key=b"too-short")

    def test_with_env_variable(self) -> None:
        key = os.urandom(32)
        env_value = key.hex()

        original = os.environ.get("LANGGRAPH_TEMPORAL_ENCRYPTION_KEY")
        try:
            os.environ["LANGGRAPH_TEMPORAL_ENCRYPTION_KEY"] = env_value
            codec = EncryptionCodec()
            assert codec._key == key
        finally:
            if original is not None:
                os.environ["LANGGRAPH_TEMPORAL_ENCRYPTION_KEY"] = original
            else:
                os.environ.pop("LANGGRAPH_TEMPORAL_ENCRYPTION_KEY", None)

    def test_no_key_raises(self) -> None:
        original = os.environ.pop("LANGGRAPH_TEMPORAL_ENCRYPTION_KEY", None)
        try:
            with pytest.raises(ValueError, match="Encryption key required"):
                EncryptionCodec()
        finally:
            if original is not None:
                os.environ["LANGGRAPH_TEMPORAL_ENCRYPTION_KEY"] = original

    def test_custom_key_id(self) -> None:
        key = os.urandom(32)
        codec = EncryptionCodec(key=key, key_id="my-key-v2")
        assert codec._key_id == "my-key-v2"
