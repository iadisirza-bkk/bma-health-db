"""Team — lightweight container for a group of agents."""
from __future__ import annotations

from agents.core.agent import Agent


class Team:
    """A group of named agents that collaborate on a request."""

    def __init__(self, agents: dict[str, Agent]):
        self.agents = agents

    def get_agent(self, name: str) -> Agent | None:
        return self.agents.get(name)
