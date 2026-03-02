"""Large payload codec for handling >2MB payloads in Temporal.

Temporal has a default payload size limit of 2MB. This codec automatically
detects oversized payloads and stores them in an external blob store,
replacing the payload data with a reference.

Usage:
    ```python
    from langgraph.temporal._codec import LargePayloadCodec

    codec = LargePayloadCodec(store=S3BlobStore(bucket="my-bucket"))
    client = await Client.connect(
        "localhost:7233",
        data_converter=DataConverter(payload_codec=codec),
    )
    ```
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

# Default threshold: 2MB (Temporal's default payload size limit)
DEFAULT_SIZE_THRESHOLD = 2 * 1024 * 1024


class BlobStore(ABC):
    """Abstract blob store for large payload offloading."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> str:
        """Store data and return a reference key.

        Args:
            key: Unique key for the data.
            data: Raw bytes to store.

        Returns:
            The key under which the data was stored.
        """
        ...

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Retrieve data by reference key.

        Args:
            key: The key returned by `put()`.

        Returns:
            The raw bytes.

        Raises:
            KeyError: If the key is not found.
        """
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete data by reference key.

        Args:
            key: The key to delete.
        """
        ...


class InMemoryBlobStore(BlobStore):
    """In-memory blob store for testing.

    Not suitable for production use - data is lost on process restart.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> str:
        self._store[key] = data
        return key

    async def get(self, key: str) -> bytes:
        if key not in self._store:
            raise KeyError(f"Blob not found: {key}")
        return self._store[key]

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


class LargePayloadCodec(PayloadCodec):
    """Temporal PayloadCodec that offloads large payloads to a blob store.

    Payloads exceeding `size_threshold` bytes are stored in the blob store,
    and replaced with a reference payload in Temporal's event history.

    Args:
        store: A `BlobStore` implementation for storing large payloads.
        size_threshold: Payloads larger than this (in bytes) are offloaded.
            Defaults to 2MB.
        prefix: Key prefix for stored blobs.
    """

    ENCODING = "binary/blob-ref"
    METADATA_BLOB_KEY = "blob-key"

    def __init__(
        self,
        store: BlobStore,
        *,
        size_threshold: int = DEFAULT_SIZE_THRESHOLD,
        prefix: str = "langgraph-temporal",
    ) -> None:
        self._store = store
        self._size_threshold = size_threshold
        self._prefix = prefix

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Encode payloads, offloading large ones to blob store."""
        result = []
        for payload in payloads:
            serialized = payload.SerializeToString()
            if len(serialized) > self._size_threshold:
                result.append(await self._offload_payload(payload, serialized))
            else:
                result.append(payload)
        return result

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Decode payloads, fetching offloaded ones from blob store."""
        result = []
        for payload in payloads:
            encoding = payload.metadata.get("encoding", b"").decode()
            if encoding == self.ENCODING:
                result.append(await self._fetch_payload(payload))
            else:
                result.append(payload)
        return result

    async def _offload_payload(self, payload: Payload, serialized: bytes) -> Payload:
        """Store a large payload in the blob store and return a reference."""
        content_hash = hashlib.sha256(serialized).hexdigest()[:16]
        blob_key = f"{self._prefix}/{uuid.uuid4().hex[:12]}-{content_hash}"

        await self._store.put(blob_key, serialized)

        return Payload(
            metadata={
                "encoding": self.ENCODING.encode(),
                self.METADATA_BLOB_KEY: blob_key.encode(),
            },
            data=b"",
        )

    async def _fetch_payload(self, payload: Payload) -> Payload:
        """Fetch a large payload from the blob store."""
        blob_key = payload.metadata.get(self.METADATA_BLOB_KEY, b"").decode()
        if not blob_key:
            return payload

        data = await self._store.get(blob_key)
        result = Payload()
        result.ParseFromString(data)
        return result
