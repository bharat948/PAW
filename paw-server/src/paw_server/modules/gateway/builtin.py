"""The built-in tool specs. Handlers are attached where they are implemented (see tools.py)."""

from paw_server.modules.gateway.registry import ToolRegistry, ToolSpec

NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def builtin_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="paw_system_status",
            description="Report whether PAW's database, storage and agent runtime are healthy.",
            input_schema=NO_ARGS,
        ),
        ToolSpec(
            name="paw_run_status",
            description="State of the current run or of the previous run in this conversation (state, error code, "
            "token usage, when it finished).",
            input_schema={
                "type": "object",
                "properties": {
                    "which": {
                        "type": "string",
                        "enum": ["current", "previous"],
                        "default": "previous",
                    }
                },
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="docs_search",
            description="Search the owner's uploaded documents and return the best matching passages with sources.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="docs_get_source",
            description="Fetch the full text of one passage returned by docs_search.",
            input_schema={
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "format": "uuid",
                        "pattern": "^[0-9a-fA-F-]{36}$",
                    }
                },
                "required": ["chunk_id"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="calendar_list_events",
            description="List the owner's upcoming Google Calendar events (title, start, end, location). Read-only.",
            input_schema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 31, "default": 7},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "additionalProperties": False,
            },
            provider="google_calendar",
        ),
    ]


def builtin_registry() -> ToolRegistry:
    return ToolRegistry(builtin_specs())
