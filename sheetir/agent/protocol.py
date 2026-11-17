"""The agent's action format (design §7.2): one JSON object per step, independent of the backend's native tool calling."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tool = Literal["search_catalog", "describe_table", "run_sql", "make_chart", "final_answer"]


class AgentAction(BaseModel):
    thought: str = Field(description="1-3 sentences: what you know and why this step")
    tool: Tool
    args: dict = Field(default_factory=dict, description="the tool's arguments")


class Step(BaseModel):
    """One step of a run, as the UI and the trace show it."""
    n: int
    thought: str
    tool: str
    args: dict
    result: dict           # what the tool returned (rows cut for display)
    seconds: float
