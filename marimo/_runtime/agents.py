# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

from typing import Optional

from marimo._ai.agents import Agent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(
        self,
        agent: Agent,
    ) -> None:
        self._agents[agent.name] = agent

    def get_agent(
        self,
        name: Optional[str],
    ) -> Agent:
        if name:
            if name not in self._agents:
                raise ValueError(f"Agent name '{name}' is not registered.")
            return self._agents[name]
        else:
            if len(self._agents) == 0:
                return None
            elif len(self._agents) > 1:
                raise ValueError(
                    "No agent name provided and multiple agents are registered."
                )
            return list(self._agents.values())[0]
