# Large Payloads

Temporal has a default payload size limit of 2MB. AI agent workflows can exceed this — for example, when channel state includes large documents, images, or conversation histories.

`LargePayloadCodec` automatically offloads oversized payloads to an external blob store.

## Setup

```python
from temporalio.client import Client
from temporalio.converter import DataConverter
from langgraph.temporal._codec import LargePayloadCodec, InMemoryBlobStore

# For production, implement BlobStore with S3, GCS, etc.
store = InMemoryBlobStore()  # testing only

codec = LargePayloadCodec(store=store)

client = await Client.connect(
    "localhost:7233",
    data_converter=DataConverter(payload_codec=codec),
)
```

## How it works

1. Before a payload enters Temporal's event history, the codec checks its size
2. If the payload exceeds the threshold (default: 2MB), it's stored in the blob store
3. A small reference payload replaces the original in the event history
4. On decode, the codec fetches the original data from the blob store

## Custom blob store

Implement `BlobStore` for your storage backend:

```python
from langgraph.temporal._codec import BlobStore


class S3BlobStore(BlobStore):
    def __init__(self, bucket: str):
        self.bucket = bucket

    async def put(self, key: str, data: bytes) -> str:
        await s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    async def get(self, key: str) -> bytes:
        response = await s3.get_object(Bucket=self.bucket, Key=key)
        return await response["Body"].read()

    async def delete(self, key: str) -> None:
        await s3.delete_object(Bucket=self.bucket, Key=key)
```

## Configuring the threshold

```python
codec = LargePayloadCodec(
    store=store,
    size_threshold=1 * 1024 * 1024,  # 1MB
)
```

## Combining with encryption

Chain codecs by applying encryption first, then large payload handling:

```python
from langgraph.temporal.encryption import EncryptionCodec

# Apply both: encrypt first, then offload if large
encryption = EncryptionCodec(key=my_key)
large_payload = LargePayloadCodec(store=store)

# Use Temporal's codec chain
client = await Client.connect(
    "localhost:7233",
    data_converter=DataConverter(
        payload_codec=encryption,  # applied first
    ),
)
```
