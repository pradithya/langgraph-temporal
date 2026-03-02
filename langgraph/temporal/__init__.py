"""LangGraph-Temporal integration for durable execution of AI agent workflows.

This package provides a Temporal backend for LangGraph, enabling:
- True durable execution with automatic recovery from failures
- Distributed node execution across worker pools
- Zero-resource human-in-the-loop via Temporal Signals
- Complete event-sourced audit trail via Temporal Event History
"""

from langgraph.temporal.checkpoint import TemporalCheckpointSaver
from langgraph.temporal.config import ActivityOptions
from langgraph.temporal.graph import TemporalGraph
from langgraph.temporal.streaming import PollingStreamBackend, StreamBackend
from langgraph.temporal.tools import activity_as_tool

__all__ = [
    "TemporalGraph",
    "ActivityOptions",
    "StreamBackend",
    "PollingStreamBackend",
    "TemporalCheckpointSaver",
    "activity_as_tool",
]
