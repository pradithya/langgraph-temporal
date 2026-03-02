"""Streaming backend for the LangGraph-Temporal integration.

Provides a pluggable streaming interface for receiving graph execution
events from Temporal Workflows.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from langgraph.temporal.config import StreamQueryResult


class StreamBackend(ABC):
    """Abstract base class for stream backends.

    Stream backends provide the mechanism for transmitting execution events
    from the Temporal Workflow to the client across process boundaries.
    """

    @abstractmethod
    async def publish(self, workflow_id: str, event: Any) -> None:
        """Publish a stream event.

        Args:
            workflow_id: The Temporal Workflow ID.
            event: The stream event to publish.
        """

    @abstractmethod
    def subscribe(self, workflow_id: str) -> AsyncIterator[Any]:
        """Subscribe to stream events for a workflow.

        Args:
            workflow_id: The Temporal Workflow ID.

        Yields:
            Stream events as they arrive.
        """


class PollingStreamBackend(StreamBackend):
    """Stream backend using Temporal Query with cursor-based pagination.

    This is the default streaming implementation. It polls the Workflow's
    `get_stream_buffer` query at regular intervals to retrieve new events.

    Args:
        poll_interval: Seconds between polls. Default 0.1.
    """

    def __init__(self, poll_interval: float = 0.1) -> None:
        self.poll_interval = poll_interval

    async def publish(self, workflow_id: str, event: Any) -> None:
        """No-op for polling backend. Events are stored in workflow state."""

    async def subscribe(self, workflow_id: str) -> AsyncIterator[Any]:
        """Poll the workflow's stream buffer query.

        This method is designed to be used with a WorkflowHandle. The actual
        polling is done in `poll_stream` which accepts the handle directly.
        """
        raise NotImplementedError("Use poll_stream() with a WorkflowHandle instead")
        # Make this an async generator so the return type is correct
        yield  # pragma: no cover

    async def poll_stream(
        self,
        handle: Any,
        query_method: Any,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[Any]:
        """Poll a workflow's stream buffer using Temporal Query.

        Args:
            handle: The Temporal WorkflowHandle.
            query_method: The query method reference (e.g.,
                `LangGraphWorkflow.get_stream_buffer`).
            timeout: Maximum time to poll before stopping.

        Yields:
            Stream events as they arrive.
        """
        cursor = 0
        start_time = time.monotonic()

        while True:
            if timeout and (time.monotonic() - start_time) > timeout:
                break

            try:
                result: StreamQueryResult = await handle.query(query_method, cursor)
                for event in result.events:
                    yield event
                cursor = result.next_cursor
            except Exception:
                # Workflow may have completed
                break

            await asyncio.sleep(self.poll_interval)
