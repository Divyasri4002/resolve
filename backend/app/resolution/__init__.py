"""
Server-side resolution orchestration (Task 13).

The package holds the one flow that connects the existing controlled executors — refund,
replacement, return — behind the existing Authority Engine. It adds no capability of its own:
every provider interaction still happens inside those executors.
"""
from app.resolution.orchestrator import (
    load_resolution,
    resolve_issue,
    resolution_response,
)

__all__ = ["load_resolution", "resolve_issue", "resolution_response"]
