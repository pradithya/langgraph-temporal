"""Tests for worker creation helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.worker import create_worker


class TestCreateWorker:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @patch("langgraph.temporal.worker.Worker")
    def test_create_worker_defaults(self, mock_worker_cls: MagicMock) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test_graph"
        mock_client = MagicMock()

        create_worker(mock_graph, mock_client)

        mock_worker_cls.assert_called_once()
        call_kwargs = mock_worker_cls.call_args
        assert call_kwargs[1]["task_queue"] == "langgraph-default"

    @patch("langgraph.temporal.worker.Worker")
    def test_create_worker_custom_queue(self, mock_worker_cls: MagicMock) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test_graph"
        mock_client = MagicMock()

        create_worker(mock_graph, mock_client, task_queue="custom-queue")

        call_kwargs = mock_worker_cls.call_args
        assert call_kwargs[1]["task_queue"] == "custom-queue"

    @patch("langgraph.temporal.worker.Worker")
    def test_create_worker_registers_graph(self, mock_worker_cls: MagicMock) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test_graph"
        mock_client = MagicMock()

        create_worker(mock_graph, mock_client)

        # Graph should be in registry
        registry = GraphRegistry.get_instance()
        # At least one graph should be registered
        assert len(registry._graphs) > 0

    @patch("langgraph.temporal.worker.Worker")
    def test_create_worker_passes_kwargs(self, mock_worker_cls: MagicMock) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test_graph"
        mock_client = MagicMock()

        create_worker(
            mock_graph,
            mock_client,
            max_concurrent_activities=10,
            max_concurrent_workflow_tasks=5,
        )

        call_kwargs = mock_worker_cls.call_args
        assert call_kwargs[1]["max_concurrent_activities"] == 10
        assert call_kwargs[1]["max_concurrent_workflow_tasks"] == 5
