"""LangGraph AsyncSqliteSaver — single async checkpointer for the whole process.

Both _run_review (graph.astream) and the stream endpoint (graph.astream_events)
call async checkpointer methods internally, so SqliteSaver (sync) does not work
with either. AsyncSqliteSaver is required for both.

AsyncSqliteSaver.from_conn_string() is a context manager — calling it bare
returns a _GeneratorContextManager, not a saver. We open aiosqlite directly.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from argus.config import get_settings

_checkpointer: AsyncSqliteSaver | None = None


async def get_checkpointer(db_path: str | None = None) -> AsyncSqliteSaver:
    """Return the process-wide AsyncSqliteSaver singleton (lazy init)."""
    global _checkpointer
    if _checkpointer is None:
        settings = get_settings()
        path = db_path or settings.checkpoints_db_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        _checkpointer = AsyncSqliteSaver(conn)
        await _checkpointer.setup()
    return _checkpointer


# Keep old name working for any other callers
get_async_checkpointer = get_checkpointer