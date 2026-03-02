"""Tests for TemporalCheckpointSaver."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from langgraph.temporal.checkpoint import TemporalCheckpointSaver
from langgraph.temporal.config import StateQueryResult


class TestTemporalCheckpointSaver:
    def test_init(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)
        assert saver.client is mock_client

    @pytest.mark.asyncio
    async def test_aget_tuple_success(self) -> None:
        mock_client = MagicMock()
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            return_value=StateQueryResult(
                channel_values={"messages": ["hello"]},
                channel_versions={"messages": 3},
                versions_seen={"agent": {"messages": 2}},
                step=5,
                status="running",
                interrupts=[],
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        result = await saver.aget_tuple(config)  # type: ignore[arg-type]

        assert result is not None
        assert result.checkpoint["channel_values"] == {"messages": ["hello"]}
        assert result.checkpoint["channel_versions"] == {"messages": 3}
        assert result.metadata["step"] == 5

    @pytest.mark.asyncio
    async def test_aget_tuple_not_found(self) -> None:
        from temporalio.service import RPCError, RPCStatusCode

        mock_client = MagicMock()
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            side_effect=RPCError(
                message="not found",
                status=RPCStatusCode.NOT_FOUND,
                raw_grpc_status=b"",
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "nonexistent"}}
        result = await saver.aget_tuple(config)  # type: ignore[arg-type]

        assert result is None

    @pytest.mark.asyncio
    async def test_aget_tuple_propagates_other_errors(self) -> None:
        from temporalio.service import RPCError, RPCStatusCode

        mock_client = MagicMock()
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            side_effect=RPCError(
                message="internal error",
                status=RPCStatusCode.INTERNAL,
                raw_grpc_status=b"",
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        with pytest.raises(RPCError):
            await saver.aget_tuple(config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_aput_is_noop(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test"}}
        result = await saver.aput(config, {}, {}, {})  # type: ignore[arg-type]
        assert result is config

    @pytest.mark.asyncio
    async def test_aput_writes_is_noop(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test"}}
        await saver.aput_writes(config, [], "task-1")  # type: ignore[arg-type]
        # Should not raise

    def test_put_is_noop(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test"}}
        result = saver.put(config, {}, {}, {})  # type: ignore[arg-type]
        assert result is config

    def test_put_writes_is_noop(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test"}}
        saver.put_writes(config, [], "task-1")  # type: ignore[arg-type]
        # Should not raise

    @pytest.mark.asyncio
    async def test_alist(self) -> None:
        mock_client = MagicMock()
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            return_value=StateQueryResult(
                channel_values={"key": "val"},
                channel_versions={"key": 1},
                versions_seen={},
                step=1,
                status="done",
                interrupts=[],
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        saver = TemporalCheckpointSaver(mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        results = []
        async for item in saver.alist(config):  # type: ignore[arg-type]
            results.append(item)

        assert len(results) == 1
        assert results[0].checkpoint["channel_values"] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_alist_none_config(self) -> None:
        mock_client = MagicMock()
        saver = TemporalCheckpointSaver(mock_client)

        results = []
        async for item in saver.alist(None):
            results.append(item)

        assert len(results) == 0
