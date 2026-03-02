"""Tests for the activity_as_tool helper."""

from __future__ import annotations

from typing import Any

import pytest

from langgraph.temporal.tools import activity_as_tool


class TestActivityAsTool:
    @pytest.mark.asyncio
    async def test_basic_wrapping(self) -> None:
        """Test wrapping a simple async function as a tool."""

        async def my_activity(query: str) -> str:
            """Search the web."""
            return f"result: {query}"

        tool = activity_as_tool(my_activity)

        result = await tool("test query")
        assert result == "result: test query"

    @pytest.mark.asyncio
    async def test_preserves_name(self) -> None:
        """Test that the tool preserves the function name."""

        async def search_web(query: str) -> str:
            """Search the web."""
            return query

        tool = activity_as_tool(search_web)
        assert tool.__name__ == "search_web"

    @pytest.mark.asyncio
    async def test_custom_name(self) -> None:
        """Test that a custom name overrides the function name."""

        async def my_fn(x: int) -> int:
            return x

        tool = activity_as_tool(my_fn, name="custom_tool")
        assert tool.__name__ == "custom_tool"

    @pytest.mark.asyncio
    async def test_custom_description(self) -> None:
        """Test that a custom description overrides the docstring."""

        async def my_fn(x: int) -> int:
            """Original description."""
            return x

        tool = activity_as_tool(my_fn, description="Custom description")
        assert tool.__doc__ == "Custom description"

    @pytest.mark.asyncio
    async def test_preserves_docstring(self) -> None:
        """Test that the tool preserves the original docstring."""

        async def my_fn(x: int) -> int:
            """My docstring."""
            return x

        tool = activity_as_tool(my_fn)
        assert tool.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_stores_activity_fn(self) -> None:
        """Test that the original activity function is accessible."""

        async def my_fn(x: int) -> int:
            return x

        tool = activity_as_tool(my_fn)
        assert tool._activity_fn is my_fn  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stores_task_queue(self) -> None:
        """Test that the task queue is stored."""

        async def my_fn(x: int) -> int:
            return x

        tool = activity_as_tool(my_fn, task_queue="gpu-queue")
        assert tool._task_queue == "gpu-queue"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_with_kwargs(self) -> None:
        """Test that kwargs are passed through."""

        async def my_fn(x: int, multiplier: int = 2) -> int:
            return x * multiplier

        tool = activity_as_tool(my_fn)
        result = await tool(3, multiplier=5)
        assert result == 15

    @pytest.mark.asyncio
    async def test_no_docstring(self) -> None:
        """Test wrapping a function with no docstring."""

        async def bare_fn(x: Any) -> Any:
            return x

        bare_fn.__doc__ = None
        tool = activity_as_tool(bare_fn)
        assert tool.__doc__ == ""
