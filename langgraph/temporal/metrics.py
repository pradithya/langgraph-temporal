"""LangGraph-specific metrics for Temporal integration.

Provides metrics collectors for monitoring LangGraph workflow execution
on Temporal, including per-node execution duration, channel state size,
interrupt wait duration, and serialization time.

Supports OpenTelemetry for distributed tracing integration.

Usage:
    ```python
    from langgraph.temporal.metrics import MetricsReporter

    reporter = MetricsReporter()
    reporter.record_node_execution("agent", duration_ms=150.5)
    reporter.record_step_completion(step=3, channel_count=5)
    ```
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("langgraph.temporal.metrics")


class MetricsReporter:
    """Collects and reports LangGraph-specific execution metrics.

    Metrics are emitted via structured logging by default. When
    OpenTelemetry is available, metrics are also recorded as OTel
    measurements.

    Attributes:
        node_durations: Per-node execution duration history (ms).
        step_count: Total steps executed.
        interrupt_count: Total interrupts encountered.
    """

    def __init__(
        self,
        *,
        workflow_id: str = "",
        enable_otel: bool = False,
    ) -> None:
        self.workflow_id = workflow_id
        self.node_durations: dict[str, list[float]] = defaultdict(list)
        self.step_count: int = 0
        self.interrupt_count: int = 0
        self._enable_otel = enable_otel
        self._meter: Any = None

        if enable_otel:
            self._setup_otel()

    def _setup_otel(self) -> None:
        """Set up OpenTelemetry meter and instruments."""
        try:
            from opentelemetry import metrics  # type: ignore[import-not-found]

            self._meter = metrics.get_meter("langgraph.temporal")
            self._node_duration_histogram = self._meter.create_histogram(
                name="langgraph.node.duration",
                description="Node execution duration in milliseconds",
                unit="ms",
            )
            self._step_counter = self._meter.create_counter(
                name="langgraph.steps.total",
                description="Total workflow steps executed",
            )
            self._interrupt_counter = self._meter.create_counter(
                name="langgraph.interrupts.total",
                description="Total interrupts encountered",
            )
        except ImportError:
            logger.warning(
                "OpenTelemetry not available. Install with: "
                "pip install opentelemetry-api opentelemetry-sdk"
            )
            self._enable_otel = False

    def record_node_execution(
        self,
        node_name: str,
        duration_ms: float,
        *,
        success: bool = True,
    ) -> None:
        """Record a node execution duration.

        Args:
            node_name: Name of the executed node.
            duration_ms: Execution duration in milliseconds.
            success: Whether the execution succeeded.
        """
        self.node_durations[node_name].append(duration_ms)

        logger.info(
            "node_execution",
            extra={
                "workflow_id": self.workflow_id,
                "node_name": node_name,
                "duration_ms": duration_ms,
                "success": success,
            },
        )

        if self._enable_otel and self._meter:
            self._node_duration_histogram.record(
                duration_ms,
                attributes={
                    "node_name": node_name,
                    "workflow_id": self.workflow_id,
                    "success": str(success),
                },
            )

    def record_step_completion(
        self,
        step: int,
        channel_count: int = 0,
    ) -> None:
        """Record a workflow step completion.

        Args:
            step: The step number that completed.
            channel_count: Number of channels with values.
        """
        self.step_count = step

        logger.info(
            "step_completion",
            extra={
                "workflow_id": self.workflow_id,
                "step": step,
                "channel_count": channel_count,
            },
        )

        if self._enable_otel and self._meter:
            self._step_counter.add(
                1,
                attributes={"workflow_id": self.workflow_id},
            )

    def record_interrupt(
        self,
        node_name: str,
        interrupt_type: str = "node",
    ) -> None:
        """Record an interrupt event.

        Args:
            node_name: Name of the node that triggered the interrupt.
            interrupt_type: Type of interrupt (node, before, after).
        """
        self.interrupt_count += 1

        logger.info(
            "interrupt",
            extra={
                "workflow_id": self.workflow_id,
                "node_name": node_name,
                "interrupt_type": interrupt_type,
            },
        )

        if self._enable_otel and self._meter:
            self._interrupt_counter.add(
                1,
                attributes={
                    "workflow_id": self.workflow_id,
                    "node_name": node_name,
                    "interrupt_type": interrupt_type,
                },
            )

    @contextmanager
    def measure_node(self, node_name: str) -> Any:
        """Context manager to measure node execution duration.

        Usage:
            ```python
            with reporter.measure_node("agent"):
                result = await execute_node(...)
            ```
        """
        start = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            self.record_node_execution(node_name, duration_ms)

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of collected metrics.

        Returns:
            Dictionary with metrics summary.
        """
        summary: dict[str, Any] = {
            "workflow_id": self.workflow_id,
            "step_count": self.step_count,
            "interrupt_count": self.interrupt_count,
            "nodes": {},
        }

        for node_name, durations in self.node_durations.items():
            summary["nodes"][node_name] = {
                "count": len(durations),
                "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
                "max_duration_ms": max(durations) if durations else 0,
                "min_duration_ms": min(durations) if durations else 0,
            }

        return summary
