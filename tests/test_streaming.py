"""Tests for streaming backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from langgraph.temporal.config import StreamQueryResult
from langgraph.temporal.streaming import PollingStreamBackend, StreamBackend


class TestStreamBackendABC:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            StreamBackend()  # type: ignore[abstract]


class TestPollingStreamBackend:
    def test_default_poll_interval(self) -> None:
        backend = PollingStreamBackend()
        assert backend.poll_interval == 0.1

    def test_custom_poll_interval(self) -> None:
        backend = PollingStreamBackend(poll_interval=0.5)
        assert backend.poll_interval == 0.5

    @pytest.mark.asyncio
    async def test_publish_is_noop(self) -> None:
        backend = PollingStreamBackend()
        await backend.publish("wf-1", {"data": "event"})
        # Should not raise

    @pytest.mark.asyncio
    async def test_subscribe_raises(self) -> None:
        backend = PollingStreamBackend()
        with pytest.raises(NotImplementedError):
            async for _ in backend.subscribe("wf-1"):
                pass

    @pytest.mark.asyncio
    async def test_poll_stream(self) -> None:
        backend = PollingStreamBackend(poll_interval=0.01)

        # Mock handle that returns events then raises to stop
        call_count = 0

        async def mock_query(method: object, cursor: int) -> StreamQueryResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return StreamQueryResult(
                    events=["event1", "event2"],
                    next_cursor=2,
                )
            elif call_count == 2:
                return StreamQueryResult(
                    events=["event3"],
                    next_cursor=3,
                )
            else:
                raise Exception("Workflow completed")

        mock_handle = AsyncMock()
        mock_handle.query = mock_query

        events = []
        async for event in backend.poll_stream(
            mock_handle, "get_stream_buffer", timeout=1.0
        ):
            events.append(event)

        assert events == ["event1", "event2", "event3"]

    @pytest.mark.asyncio
    async def test_poll_stream_timeout(self) -> None:
        backend = PollingStreamBackend(poll_interval=0.01)

        async def mock_query(method: object, cursor: int) -> StreamQueryResult:
            return StreamQueryResult(events=[], next_cursor=0)

        mock_handle = AsyncMock()
        mock_handle.query = mock_query

        events = []
        async for event in backend.poll_stream(
            mock_handle, "get_stream_buffer", timeout=0.05
        ):
            events.append(event)

        # Should eventually timeout with no events
        assert events == []
