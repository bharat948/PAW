"""Fixed retrieval evaluation corpus. Re-run whenever chunking or the embedding model changes (001C-R06)."""

from tests import fixtures as fx

DOCS: dict[str, bytes] = {
    "space.md": b"# Observatory\nThe observatory recorded quasar telemetry every night.\n\n"
    b"## Calibration\nSensors are calibrated against a reference pulsar.\n",
    "cooking.txt": b"Sourdough bread needs a healthy fermentation starter. Feed the starter daily "
    b"with flour and water, then let the dough proof overnight.",
    "people.csv": fx.CSV_DOC,
    "finance.pdf": fx.text_pdf(
        ["Quarterly revenue grew in Helsinki and Tallinn", "Appendix methodology"]
    ),
    "markup.txt": b"Widget manual. <script>alert('x')</script> <b>bold</b> assembly instructions.",
}

# (kind, query, expected top document or None for "no results")
QUERIES = [
    ("keyword", "sourdough starter", "cooking.txt"),
    ("keyword", "quasar telemetry", "space.md"),
    ("keyword", "Linus kernel", "people.csv"),
    ("keyword", "quarterly revenue Helsinki", "finance.pdf"),
    ("hybrid", "feeding the dough with flour", "cooking.txt"),
    ("markup", "script alert widget", "markup.txt"),
    ("no_match", "zzzqqq xylophone nonexistentterm", None),
]
ADVERSARIAL = [
    "'; DROP TABLE documents; --",
    "a & | ! ( ) :*",
    "<script>alert(1)</script>",
    '"unterminated',
    "\\",
    "%" * 50,
    "😀 日本語 query",
]
