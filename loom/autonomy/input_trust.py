"""Load-time external furniture filter; runtime verifies native executors."""

READ_TOOLS = frozenset({"read_file", "list_dir", "load_skill", "weather"})
EXTERNAL_TOOLS = READ_TOOLS | {"journal_append"}
