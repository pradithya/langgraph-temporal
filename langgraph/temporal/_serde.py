"""Serialization helpers for the LangGraph-Temporal integration.

Handles serialization/deserialization of channel state, writes, and
special values (Overwrite, UntrackedValue) across the Temporal Activity boundary.
"""

from __future__ import annotations

from typing import Any

from langgraph.channels.untracked_value import UntrackedValue
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


class StateSerializer:
    """Serializes and deserializes LangGraph channel state for Temporal.

    Wraps `JsonPlusSerializer` to handle the Activity boundary where state
    must be serialized into Temporal's event history.
    """

    def __init__(self) -> None:
        self._serde = JsonPlusSerializer()

    def serialize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Serialize channel values for transmission across Activity boundary.

        Args:
            state: Channel name to value mapping.

        Returns:
            Serialized state dict with typed encoding.
        """
        result: dict[str, Any] = {}
        for key, value in state.items():
            type_tag, data = self._serde.dumps_typed(value)
            result[key] = {"type": type_tag, "data": data.hex() if data else ""}
        return result

    def deserialize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Deserialize channel values from Activity boundary.

        Args:
            state: Serialized state dict with typed encoding.

        Returns:
            Channel name to value mapping.
        """
        result: dict[str, Any] = {}
        for key, value in state.items():
            if isinstance(value, dict) and "type" in value and "data" in value:
                data = bytes.fromhex(value["data"]) if value["data"] else b""
                result[key] = self._serde.loads_typed((value["type"], data))
            else:
                result[key] = value
        return result

    def serialize_writes(self, writes: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Serialize channel writes for transmission.

        Args:
            writes: List of (channel_name, value) tuples.

        Returns:
            Serialized writes with typed encoding.
        """
        result = []
        for channel, value in writes:
            type_tag, data = self._serde.dumps_typed(value)
            result.append(
                (channel, {"type": type_tag, "data": data.hex() if data else ""})
            )
        return result

    def deserialize_writes(
        self, writes: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """Deserialize channel writes from Activity boundary.

        Args:
            writes: Serialized writes with typed encoding.

        Returns:
            List of (channel_name, value) tuples.
        """
        result = []
        for channel, value in writes:
            if isinstance(value, dict) and "type" in value and "data" in value:
                data = bytes.fromhex(value["data"]) if value["data"] else b""
                result.append((channel, self._serde.loads_typed((value["type"], data))))
            else:
                result.append((channel, value))
        return result


def is_untracked_channel(
    channel_name: str,
    channel_specs: dict[str, Any],
) -> bool:
    """Check if a channel is an UntrackedValue channel.

    UntrackedValue writes must never appear in Temporal event history.

    Args:
        channel_name: The channel name to check.
        channel_specs: The graph's channel specifications.

    Returns:
        True if the channel is an UntrackedValue.
    """
    spec = channel_specs.get(channel_name)
    return isinstance(spec, UntrackedValue)


def filter_untracked_writes(
    writes: list[tuple[str, Any]],
    channel_specs: dict[str, Any],
) -> list[tuple[str, Any]]:
    """Filter out writes to UntrackedValue channels.

    Args:
        writes: List of (channel_name, value) tuples.
        channel_specs: The graph's channel specifications.

    Returns:
        Writes with UntrackedValue channels removed.
    """
    return [
        (ch, val) for ch, val in writes if not is_untracked_channel(ch, channel_specs)
    ]
