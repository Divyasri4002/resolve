"""
The strict data shapes of one bounded agent step (Task CORE-2, Phase 3 and 15).

A reasoning model's answer to "what should happen next?" is parsed into exactly one of these two
forms before any code looks at it:

    {"action": "tool", "tool_name": "select_product", "arguments": {"product_id": "..."}}
    {"action": "respond", "message": "..."}

`extra="forbid"` and the cross-field validator are the whole point. A response that carries an
undeclared key — `amount`, `user_id`, `authority`, `shopify_variant_gid`, a provider reference — is
rejected at this boundary rather than being quietly tolerated because the field "was not used
anyway". And `action` is a two-value literal: there is no third form, so there is no way to express
"run this code" or "make this HTTP call" to RESOLVE's agent.

Nothing in this module executes anything. It is the shape of a proposal, and the proposal still has
to clear the declared tool surface (`app.agents.tools`), the server-side argument schemas, and the
Authority Engine.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MESSAGE_CHARS = 4000


class AgentStepDecision(BaseModel):
    """One step of agent reasoning: name a declared tool, or answer the user in text."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "respond"]
    tool_name: str | None = Field(
        default=None, max_length=64, description="A tool declared on RESOLVE's agent surface"
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)

    @model_validator(mode="after")
    def _shape_matches_action(self) -> "AgentStepDecision":
        if self.action == "tool":
            if not self.tool_name or not self.tool_name.strip():
                raise ValueError("action='tool' requires a non-empty tool_name")
        else:
            if not self.message or not self.message.strip():
                raise ValueError("action='respond' requires a non-empty message")
        return self

    @classmethod
    def tool_call(cls, tool_name: str, arguments: dict[str, Any] | None = None) -> "AgentStepDecision":
        return cls(action="tool", tool_name=tool_name, arguments=arguments or {})

    @classmethod
    def respond(cls, message: str) -> "AgentStepDecision":
        return cls(action="respond", message=message.strip()[:MAX_MESSAGE_CHARS])

    @classmethod
    def refusal(cls, reason: str) -> "AgentStepDecision":
        """
        The floor for a step that could not be understood: say something honest, do nothing.

        Used when a provider returns prose, an unknown action, or a shape this schema will not
        accept. Failing closed here means the loop stops with a message the user can read, not that
        the closest guess gets executed.
        """
        return cls.respond(
            "I could not turn that into a safe next step, so I did nothing. "
            f"{reason}".strip()[:MAX_MESSAGE_CHARS]
        )
