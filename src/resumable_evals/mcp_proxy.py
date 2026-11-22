"""MCP proxy (not written yet).

Record mode: forward tool calls to the real MCP servers and record each call
and result in order. Rebuild mode: re-execute each recorded call against the
fresh environment, one at a time, and pass the result to
``RebuildCursor.check_tool``. See docs/DESIGN.md.
"""
