"""Graph definition converter and registry for the LangGraph-Temporal integration.

Extracts serializable graph topology from compiled Pregel instances and
provides a thread-safe registry for Activity-side graph lookup.
"""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


class GraphRegistry:
    """Thread-safe singleton registry for compiled LangGraph graphs.

    Activities running concurrently on the same worker need access to the
    compiled graph to retrieve node callables, channel specs, and edge
    definitions. This registry provides that lookup by reference string.
    """

    _instance: GraphRegistry | None = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        self._graphs: dict[str, Pregel] = {}
        self._graph_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> GraphRegistry:
        """Get the singleton registry instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance. For testing only."""
        with cls._lock:
            cls._instance = None

    def register(self, graph: Pregel) -> str:
        """Register a compiled graph and return its reference string.

        Args:
            graph: A compiled Pregel graph instance.

        Returns:
            A unique reference string for retrieving the graph.
        """
        ref = f"{graph.name}_{uuid.uuid4().hex[:8]}"
        with self._graph_lock:
            self._graphs[ref] = graph
        return ref

    def get(self, ref: str) -> Pregel:
        """Retrieve a registered graph by reference.

        Args:
            ref: The reference string returned by `register()`.

        Returns:
            The compiled Pregel graph instance.

        Raises:
            KeyError: If the reference is not found.
        """
        with self._graph_lock:
            return self._graphs[ref]

    def unregister(self, ref: str) -> None:
        """Remove a graph from the registry.

        Args:
            ref: The reference string to remove.
        """
        with self._graph_lock:
            self._graphs.pop(ref, None)


def extract_graph_metadata(graph: Pregel) -> dict[str, Any]:
    """Extract metadata from a compiled Pregel graph.

    Extracts the graph topology information needed by the Workflow to
    orchestrate execution: node names, channel specs, triggers, interrupt
    configuration, and edge information.

    Args:
        graph: A compiled Pregel graph instance.

    Returns:
        Dictionary containing graph metadata.
    """
    return {
        "node_names": list(graph.nodes.keys()),
        "channel_specs": {
            name: type(spec).__name__ for name, spec in graph.channels.items()
        },
        "trigger_to_nodes": dict(graph.trigger_to_nodes),
        "interrupt_before_nodes": list(graph.interrupt_before_nodes),
        "interrupt_after_nodes": list(graph.interrupt_after_nodes),
        "input_channels": graph.input_channels,
        "output_channels": graph.output_channels,
        "stream_channels": graph.stream_channels,
    }
