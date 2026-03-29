# Metrics & Observability

langgraph-temporal provides a `MetricsReporter` for monitoring Workflow execution. It supports structured logging by default and optional OpenTelemetry integration.

## Basic usage

```python
from langgraph.temporal.metrics import MetricsReporter

reporter = MetricsReporter(workflow_id="my-workflow-1")

# Record node execution
reporter.record_node_execution("agent", duration_ms=150.5)
reporter.record_node_execution("tools", duration_ms=42.0)

# Record step completion
reporter.record_step_completion(step=3, channel_count=5)

# Record interrupts
reporter.record_interrupt("approval", interrupt_type="before")

# Get summary
summary = reporter.get_summary()
print(summary)
```

## Context manager for timing

```python
with reporter.measure_node("agent"):
    result = await execute_node(...)
# Duration is automatically recorded
```

## OpenTelemetry integration

Enable OTel metrics for distributed tracing:

```python
reporter = MetricsReporter(
    workflow_id="my-workflow-1",
    enable_otel=True,
)
```

This creates the following OTel instruments:

| Metric | Type | Description |
|---|---|---|
| `langgraph.node.duration` | Histogram | Node execution duration (ms) |
| `langgraph.steps.total` | Counter | Total workflow steps |
| `langgraph.interrupts.total` | Counter | Total interrupts |

Attributes include `workflow_id`, `node_name`, and `success`.

## Temporal's built-in observability

In addition to langgraph-temporal's metrics, Temporal provides:

- **Web UI** — visual workflow execution timeline at `http://localhost:8233`
- **Event History** — complete audit trail of every Activity execution
- **Workflow Queries** — query current state from any process
- **Search Attributes** — index and search workflows by custom attributes

These work out of the box with no additional configuration.
