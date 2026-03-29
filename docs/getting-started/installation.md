# Installation

## Requirements

- Python >= 3.10
- A running [Temporal](https://temporal.io/) server (for production use)

## Install from PyPI

```bash
pip install langgraph-temporal
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add langgraph-temporal
```

This installs the core dependencies:

- `langgraph >= 1.0.0`
- `temporalio >= 1.7.0`

## Optional dependencies

For **encryption** support (AES-GCM or Fernet):

```bash
pip install langgraph-temporal cryptography
```

For **OpenTelemetry** metrics:

```bash
pip install langgraph-temporal opentelemetry-api opentelemetry-sdk
```

## Running a local Temporal server

For development, you can run Temporal locally with Docker Compose. The repository includes a pre-configured setup:

```bash
git clone https://github.com/pradithya/langgraph-temporal.git
cd langgraph-temporal

cp .env.ci .env
make start_temporal
```

This starts:

- **Temporal Server** on `localhost:7233` (gRPC)
- **Temporal Web UI** on `http://localhost:8233`
- **PostgreSQL** as the persistence backend
- **Elasticsearch** for workflow visibility

To stop:

```bash
make stop_temporal
```

!!! tip "Temporal CLI"
    For a lighter-weight setup, you can also use the [Temporal CLI](https://docs.temporal.io/cli):

    ```bash
    temporal server start-dev
    ```

    This starts a single-node Temporal server at `localhost:7233` with the Web UI at `http://localhost:8233`.
