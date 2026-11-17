import random
from datetime import UTC, datetime
from uuid import UUID

from paw_server.modules.sessions.assembler import assemble_rows
from paw_server.modules.sessions.checkpoints import CLOSE, OPEN, CheckpointStream, extract


def row(seq, body, *, kind="user", run=None, forgotten=False):
    return dict(
        id=UUID(int=seq),
        session_id=UUID(int=999),
        seq=seq,
        body=body,
        data={},
        kind=kind,
        run_id=run or UUID(int=seq),
        forgotten=forgotten,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        status="complete",
    )


def test_200_generated_conversations_assemble_identically_and_change_with_their_inputs():
    rng = random.Random(201)
    for n in range(200):
        rows = [
            row(i, str(rng.randrange(10**10)), kind="user" if i % 2 else "assistant")
            for i in range(1, rng.randrange(3, 60))
        ]
        current = row(len(rows) + 1, "current", run=UUID(int=1000))
        rows.append(current)
        kw = dict(
            current_run_id=UUID(int=1000),
            profile_hash="profile",
            instructions="persona",
            revisions={"session": n},
        )
        a, b = assemble_rows(rows, **kw), assemble_rows(rows, **kw)
        assert a == b and a.hash == b.hash
        rows[0]["body"] += "changed"
        assert assemble_rows(rows, **kw).hash != a.hash
        assert a.input == "current" and "current" not in str(a.history)


def test_forgotten_long_tool_and_unfinished_output_are_rendered_honestly():
    rows = [
        row(1, "secret", forgotten=True),
        row(2, "x" * 20000, kind="tool_result"),
        row(3, "half", kind="assistant"),
        row(4, "continue"),
    ]
    rows[2]["status"] = "unfinished"
    result = assemble_rows(
        rows, current_run_id=UUID(int=4), profile_hash="p", instructions="", revisions={}
    )
    text = str(result.history)
    assert "secret" not in text and "forgotten" in text
    assert "full result: message" in text and "unfinished" in text and len(text) < 2500


def test_summary_and_history_budgets_and_current_message():
    rows = [row(i, "long " * 200) for i in range(1, 8)]
    result = assemble_rows(
        rows,
        current_run_id=UUID(int=7),
        profile_hash="p",
        instructions="",
        revisions={},
        history_budget=600,
    )
    assert len(result.history) <= 2 and result.meta["truncated"]
    result = assemble_rows(
        rows,
        current_run_id=UUID(int=7),
        profile_hash="p",
        instructions="",
        revisions={},
        summary={"id": "sum", "body": "known facts", "to_seq": 5},
        history_budget=600,
    )
    assert "known facts" in result.instructions and len(result.history) == 1
    assert result.input == rows[-1]["body"]


def test_checkpoint_stream_hides_metadata_at_every_possible_chunk_boundary():
    text = (
        "Answer."
        + OPEN
        + '{"task_id":"one","status":"completed","goal":"test","summary":"done"}'
        + CLOSE
    )
    for i in range(len(text)):
        stream = CheckpointStream()
        assert stream.feed(text[:i]) + stream.feed(text[i:]) == "Answer."
    clean, cp = extract(text)
    assert clean == "Answer." and cp is not None and cp.status == "completed"
    assert extract("Answer." + OPEN + "invalid" + CLOSE) == ("Answer.", None)
