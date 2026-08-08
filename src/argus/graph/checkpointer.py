"""LangGraph SqliteSaver checkpointer factory.

checkpoints.db is owned entirely by LangGraph — application code never
reads or writes it directly.
"""

from __future__ import annotations

from langgraph.checkpoint.sqlite import SqliteSaver

from argus.config import get_settings


def get_checkpointer(db_path: str | None = None) -> SqliteSaver:
    """Return a configured SqliteSaver for the given (or default) path."""
    settings = get_settings()
    path = db_path or settings.checkpoints_db_path
    return SqliteSaver.from_conn_string(path)
