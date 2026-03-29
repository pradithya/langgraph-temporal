# Encryption

Temporal stores all Workflow data (inputs, outputs, state) in its event history. If your agent processes PII or sensitive data, you should encrypt this data at rest.

langgraph-temporal provides two encryption codecs that implement Temporal's `PayloadCodec` protocol.

## AES-256-GCM (recommended)

```python
from temporalio.client import Client
from temporalio.converter import DataConverter
from langgraph.temporal.encryption import EncryptionCodec

# Generate a key (do this once, store securely)
# key = os.urandom(32)

codec = EncryptionCodec(key=b"your-32-byte-key-here-for-aes!!")

client = await Client.connect(
    "localhost:7233",
    data_converter=DataConverter(payload_codec=codec),
)
```

### Using environment variables

Instead of passing the key in code, set it via environment variable:

```bash
export LANGGRAPH_TEMPORAL_ENCRYPTION_KEY="hex-encoded-32-byte-key"
```

```python
codec = EncryptionCodec()  # reads from env var automatically
```

### Generating a key

```python
import os
from langgraph.temporal.encryption import generate_encryption_key

key = generate_encryption_key()  # 32 random bytes
print(key.hex())  # hex-encode for env var
```

## Fernet (simpler setup)

For simpler deployments where AES-GCM key management is overhead:

```python
from cryptography.fernet import Fernet
from langgraph.temporal.encryption import FernetEncryptionCodec

key = Fernet.generate_key()  # URL-safe base64 string
codec = FernetEncryptionCodec(key=key)
```

## What gets encrypted

When a `PayloadCodec` is configured on the Temporal client, **all** data flowing through Temporal is encrypted:

- Activity inputs and outputs (node state)
- Workflow inputs and outputs
- Signal payloads (resume values, state updates)
- Query results (state queries)

!!! note "Temporal UI"
    Encrypted payloads appear as opaque binary blobs in the Temporal Web UI. To view decrypted data in the UI, configure a [Codec Server](https://docs.temporal.io/production-deployment/data-encryption).
