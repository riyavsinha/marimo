# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional

from marimo._output.rich_help import mddoc
from marimo._runtime.context import ContextNotInitializedError, get_context


class SuggestionType(str, Enum):
    PROMPT_IDEA = "prompt_idea"
    PROMPT_WARNING = "prompt_warning"


@dataclass
class Suggestion:
    id: str
    title: str
    description: str
    type: SuggestionType

    @classmethod
    def create(
        cls, title: str, description: str, suggestion_type: SuggestionType
    ) -> "Suggestion":
        return cls(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            type=suggestion_type,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "type": self.type,
        }


@dataclass
class Agent:
    name: str
    run_fn: Callable[..., Any]
    suggestions_fn: Optional[Callable[..., List[Suggestion]]] = None


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
