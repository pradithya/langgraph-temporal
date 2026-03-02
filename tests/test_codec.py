"""Tests for the LargePayloadCodec and blob stores."""

from __future__ import annotations

import pytest

from langgraph.temporal._codec import InMemoryBlobStore, LargePayloadCodec


class TestInMemoryBlobStore:
    @pytest.mark.asyncio
    async def test_put_and_get(self) -> None:
        store = InMemoryBlobStore()
        await store.put("key1", b"hello")
        data = await store.get("key1")
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_get_missing_raises(self) -> None:
        store = InMemoryBlobStore()
        with pytest.raises(KeyError, match="Blob not found"):
            await store.get("nonexistent")

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        store = InMemoryBlobStore()
        await store.put("key1", b"data")
        await store.delete("key1")
        with pytest.raises(KeyError):
            await store.get("key1")

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self) -> None:
        store = InMemoryBlobStore()
        await store.delete("nonexistent")  # Should not raise


class TestLargePayloadCodecInit:
    def test_default_threshold(self) -> None:
        store = InMemoryBlobStore()
        codec = LargePayloadCodec(store)
        assert codec._size_threshold == 2 * 1024 * 1024

    def test_custom_threshold(self) -> None:
        store = InMemoryBlobStore()
        codec = LargePayloadCodec(store, size_threshold=1024)
        assert codec._size_threshold == 1024

    def test_custom_prefix(self) -> None:
        store = InMemoryBlobStore()
        codec = LargePayloadCodec(store, prefix="my-app")
        assert codec._prefix == "my-app"
