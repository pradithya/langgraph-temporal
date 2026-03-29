"""Tests for worker creation helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.worker import WorkerGroup, _resolve_worker_queue, create_worker


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


class TestWorkerAffinity:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @patch("langgraph.temporal.worker.Worker")
    def test_affinity_creates_worker_group(self, mock_worker_cls: MagicMock) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        result = create_worker(mock_graph, mock_client, use_worker_affinity=True)

        assert isinstance(result, WorkerGroup)
        # Two Worker instances: shared + worker-specific
        assert mock_worker_cls.call_count == 2

    @patch("langgraph.temporal.worker.Worker")
    def test_affinity_shared_worker_has_workflows(
        self, mock_worker_cls: MagicMock
    ) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        create_worker(
            mock_graph,
            mock_client,
            task_queue="shared-q",
            use_worker_affinity=True,
        )

        # First call is the shared worker
        shared_call = mock_worker_cls.call_args_list[0]
        assert shared_call[1]["task_queue"] == "shared-q"
        assert len(shared_call[1]["workflows"]) > 0
        # get_available_task_queue activity should be registered
        activity_names = [a.__name__ for a in shared_call[1]["activities"]]
        assert "get_available_task_queue" in activity_names

    @patch("langgraph.temporal.worker.Worker")
    def test_affinity_specific_worker_has_activities(
        self, mock_worker_cls: MagicMock
    ) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        create_worker(
            mock_graph,
            mock_client,
            task_queue="shared-q",
            use_worker_affinity=True,
        )

        # Second call is the worker-specific worker
        specific_call = mock_worker_cls.call_args_list[1]
        assert specific_call[1]["task_queue"].startswith("shared-q-worker-")
        activity_names = [a.__name__ for a in specific_call[1]["activities"]]
        assert "execute_node" in activity_names
        assert "dynamic_execute_node" in activity_names


class TestResolveWorkerQueue:
    def test_generates_queue_without_file(self) -> None:
        queue = _resolve_worker_queue("base", None)
        assert queue.startswith("base-worker-")

    def test_persists_and_reads_queue(self, tmp_path: MagicMock) -> None:
        queue_file = tmp_path / "worker_queue.txt"

        # First call: generates and persists
        q1 = _resolve_worker_queue("base", queue_file)
        assert q1.startswith("base-worker-")
        assert queue_file.exists()
        assert queue_file.read_text().strip() == q1

        # Second call: reads persisted value
        q2 = _resolve_worker_queue("base", queue_file)
        assert q2 == q1

    def test_generates_different_queues_without_file(self) -> None:
        q1 = _resolve_worker_queue("base", None)
        q2 = _resolve_worker_queue("base", None)
        assert q1 != q2
