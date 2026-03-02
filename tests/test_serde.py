"""Tests for serialization helpers."""

from __future__ import annotations

from langgraph.temporal._serde import (
    StateSerializer,
    filter_untracked_writes,
    is_untracked_channel,
)


class TestStateSerializer:
    def setup_method(self) -> None:
        self.serde = StateSerializer()

    def test_serialize_deserialize_state_roundtrip(self) -> None:
        state = {
            "messages": ["hello", "world"],
            "count": 42,
            "active": True,
        }
        serialized = self.serde.serialize_state(state)
        deserialized = self.serde.deserialize_state(serialized)

        assert deserialized["messages"] == ["hello", "world"]
        assert deserialized["count"] == 42
        assert deserialized["active"] is True

    def test_serialize_deserialize_writes_roundtrip(self) -> None:
        writes = [
            ("messages", ["new message"]),
            ("count", 10),
        ]
        serialized = self.serde.serialize_writes(writes)
        deserialized = self.serde.deserialize_writes(serialized)

        assert deserialized[0] == ("messages", ["new message"])
        assert deserialized[1] == ("count", 10)

    def test_serialize_none_value(self) -> None:
        state = {"key": None}
        serialized = self.serde.serialize_state(state)
        deserialized = self.serde.deserialize_state(serialized)
        assert deserialized["key"] is None

    def test_serialize_empty_state(self) -> None:
        state: dict[str, object] = {}
        serialized = self.serde.serialize_state(state)
        deserialized = self.serde.deserialize_state(serialized)
        assert deserialized == {}

    def test_deserialize_passthrough_non_typed(self) -> None:
        """Non-typed values should pass through unchanged."""
        state = {"key": "plain_value"}
        deserialized = self.serde.deserialize_state(state)
        assert deserialized["key"] == "plain_value"

    def test_deserialize_writes_passthrough_non_typed(self) -> None:
        writes = [("channel", "plain_value")]
        deserialized = self.serde.deserialize_writes(writes)
        assert deserialized[0] == ("channel", "plain_value")


class TestUntrackedFiltering:
    def test_is_untracked_channel(self) -> None:
        from langgraph.channels.untracked_value import UntrackedValue

        specs = {
            "messages": object(),  # Not UntrackedValue
            "scratch": UntrackedValue(str),
        }
        assert not is_untracked_channel("messages", specs)
        assert is_untracked_channel("scratch", specs)
        assert not is_untracked_channel("nonexistent", specs)

    def test_filter_untracked_writes(self) -> None:
        from langgraph.channels.untracked_value import UntrackedValue

        specs = {
            "messages": object(),
            "scratch": UntrackedValue(str),
        }
        writes = [
            ("messages", "hello"),
            ("scratch", "temp_data"),
            ("messages", "world"),
        ]
        filtered = filter_untracked_writes(writes, specs)
        assert len(filtered) == 2
        assert all(ch == "messages" for ch, _ in filtered)

    def test_filter_empty_writes(self) -> None:
        filtered = filter_untracked_writes([], {})
        assert filtered == []
