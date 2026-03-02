"""Tests for the MetricsReporter."""

from __future__ import annotations

from langgraph.temporal.metrics import MetricsReporter


class TestMetricsReporter:
    def test_init_defaults(self) -> None:
        reporter = MetricsReporter()
        assert reporter.workflow_id == ""
        assert reporter.step_count == 0
        assert reporter.interrupt_count == 0

    def test_init_with_workflow_id(self) -> None:
        reporter = MetricsReporter(workflow_id="wf-123")
        assert reporter.workflow_id == "wf-123"

    def test_record_node_execution(self) -> None:
        reporter = MetricsReporter(workflow_id="wf-123")
        reporter.record_node_execution("agent", 150.5)
        reporter.record_node_execution("agent", 200.0)
        reporter.record_node_execution("tools", 50.0)

        assert len(reporter.node_durations["agent"]) == 2
        assert len(reporter.node_durations["tools"]) == 1

    def test_record_step_completion(self) -> None:
        reporter = MetricsReporter()
        reporter.record_step_completion(3, channel_count=5)
        assert reporter.step_count == 3

    def test_record_interrupt(self) -> None:
        reporter = MetricsReporter()
        reporter.record_interrupt("agent", "before")
        reporter.record_interrupt("tools", "node")
        assert reporter.interrupt_count == 2

    def test_measure_node_context_manager(self) -> None:
        reporter = MetricsReporter()
        with reporter.measure_node("agent"):
            # Simulate some work
            _ = sum(range(100))

        assert len(reporter.node_durations["agent"]) == 1
        assert reporter.node_durations["agent"][0] >= 0

    def test_get_summary(self) -> None:
        reporter = MetricsReporter(workflow_id="wf-123")
        reporter.record_node_execution("agent", 100.0)
        reporter.record_node_execution("agent", 200.0)
        reporter.record_node_execution("tools", 50.0)
        reporter.record_step_completion(5)
        reporter.record_interrupt("agent")

        summary = reporter.get_summary()

        assert summary["workflow_id"] == "wf-123"
        assert summary["step_count"] == 5
        assert summary["interrupt_count"] == 1
        assert summary["nodes"]["agent"]["count"] == 2
        assert summary["nodes"]["agent"]["avg_duration_ms"] == 150.0
        assert summary["nodes"]["agent"]["max_duration_ms"] == 200.0
        assert summary["nodes"]["agent"]["min_duration_ms"] == 100.0
        assert summary["nodes"]["tools"]["count"] == 1

    def test_get_summary_empty(self) -> None:
        reporter = MetricsReporter()
        summary = reporter.get_summary()

        assert summary["step_count"] == 0
        assert summary["interrupt_count"] == 0
        assert summary["nodes"] == {}
