"""TemporalCheckpointSaver - bridges LangGraph's checkpoint API with Temporal.

Provides a read-oriented `BaseCheckpointSaver` implementation that reads
state from Temporal Workflows via Queries rather than from an external database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from temporalio.client import Client as TemporalClient
from temporalio.service import RPCError, RPCStatusCode

from langgraph.temporal.workflow import LangGraphWorkflow


class TemporalCheckpointSaver(BaseCheckpointSaver):
    """CheckpointSaver that reads state from Temporal Workflows.

    This is a read-oriented adapter. Writes are handled implicitly by
    Temporal's event history. The primary use case is enabling
    `get_state()` and `get_state_history()` to work transparently.

    Args:
        client: A Temporal client instance.
    """

    def __init__(self, client: TemporalClient) -> None:
        super().__init__()
        self.client = client

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Retrieve current state by querying the Temporal Workflow."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError("Use aget_tuple in async context")
            return loop.run_until_complete(self.aget_tuple(config))
        except RuntimeError:
            return None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Async version of get_tuple."""
        thread_id = config["configurable"]["thread_id"]
        handle = self.client.get_workflow_handle(workflow_id=thread_id)

        try:
            state = await handle.query(LangGraphWorkflow.get_current_state)
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise

        checkpoint = Checkpoint(
            v=4,
            id=str(state.step),
            ts=datetime.now(timezone.utc).isoformat(),
            channel_values=state.channel_values,
            channel_versions=state.channel_versions,
            versions_seen=state.versions_seen,
        )
        metadata = CheckpointMetadata(
            source="loop",
            step=state.step,
            parents={},
        )

        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from Temporal event history.

        Currently returns at most the current state. Full event history
        traversal (including continue-as-new chains) is planned for v0.3.
        """
        if config is None:
            return

        try:
            result = self.get_tuple(config)
            if result:
                yield result
        except Exception:
            return

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Async version of list."""
        if config is None:
            return

        try:
            result = await self.aget_tuple(config)
            if result:
                yield result
        except Exception:
            return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """No-op. Temporal handles state persistence via event history."""
        return config

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """No-op. Temporal handles state persistence via event history."""
        return config

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """No-op. Temporal handles write persistence via event history."""

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """No-op. Temporal handles write persistence via event history."""
