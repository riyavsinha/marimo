# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional

from marimo._output.rich_help import mddoc
from marimo._runtime.context import ContextNotInitializedError, get_context


class SuggestionType(str, Enum):
    IDEA = "idea"
    WARNING = "warning"
    TIP = "tip"


@dataclass
class Suggestion:
    id: str
    title: str
    description: str
    type: SuggestionType
    timestamp: float

    @classmethod
    def create(
        cls, title: str, description: str, type: SuggestionType
    ) -> "Suggestion":
        return cls(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            type=type,
            timestamp=time.time() * 1000,  # Convert to JS timestamp
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "timestamp": self.timestamp,
        }


class Agent:
    def __init__(
        self,
        name: str,
        run_fn: Callable[..., Any],
        suggestions_fn: Optional[Callable[..., List[Suggestion]]] = None,
    ):
        self.name = name
        self.run_fn = run_fn
        self.suggestions_fn = suggestions_fn


@mddoc
def register_agent(agent: Agent) -> None:
    """Register an LLM agent."""
    try:
        _registry = get_context().agent_registry
        _registry.register(agent)
    except ContextNotInitializedError:
        # Registration may be picked up later, but there is nothing to do
        # at this point.
        pass


@mddoc
async def run_agent(prompt: str, name: Optional[str] = None) -> Any:
    """
    Run an LLM agent.
    """
    try:
        _registry = get_context().agent_registry
        agent = _registry.get_agent(name)
        result = agent.run_fn(prompt)
        return result
    except ContextNotInitializedError:
        pass
