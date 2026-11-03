"""Tool names a profile may grant. The 002D gateway registry replaces this list with its own table."""

KNOWN_TOOLS = frozenset(
    {
        "calendar_list_events",
        "docs_get_source",
        "docs_search",
        "paw_run_status",
        "paw_system_status",
    }
)
