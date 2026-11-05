import pytest

from paw_server.modules.costs import tokens
from paw_server.modules.costs.pricing import CostError

MODEL = "gpt-4o-mini"


def body(**over):
    b = {"model": MODEL, "messages": [{"role": "user", "content": "Reply briefly"}]}
    b.update(over)
    return b


def test_estimate_overcounts_a_plain_message_and_grows_with_everything_the_model_reads():
    enc = tokens.encoding_for(MODEL)
    bare = len(enc.encode("Reply briefly"))
    base = tokens.estimate_input(body(), MODEL)
    assert base > bare  # framing is counted, never just the text
    tools = [
        {"type": "function", "function": {"name": "docs_search", "parameters": {"type": "object"}}}
    ]
    assert tokens.estimate_input(body(tools=tools), MODEL) > base
    history = body()["messages"] + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "docs_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert tokens.estimate_input(body(messages=history), MODEL) > base


def test_special_token_text_in_a_message_is_counted_as_text_not_an_error():
    assert (
        tokens.estimate_input(body(messages=[{"role": "user", "content": "<|endoftext|>"}]), MODEL)
        > 0
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"messages": []},
        {"messages": "hi"},
        {"messages": ["hi"]},
        {"messages": [{"role": "user", "content": {"a": 1}}]},
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
            ]
        },
    ],
)
def test_unsupported_payloads_fail_closed(bad):
    with pytest.raises(CostError) as e:
        tokens.estimate_input(bad, MODEL)
    assert e.value.code == "unsupported_payload"


def test_unknown_model_has_no_encoding():
    with pytest.raises(CostError) as e:
        tokens.estimate_input(body(), "not-a-model")
    assert e.value.code == "unsupported_model"


def test_bound_adds_margin_caps_at_context_and_rejects_what_cannot_fit():
    assert tokens.input_bound(100, 128_000) == 110 + tokens.MARGIN_TOKENS
    assert tokens.input_bound(127_900, 128_000) == 128_000
    with pytest.raises(CostError) as e:
        tokens.input_bound(128_001, 128_000)
    assert e.value.code == "input_exceeds_context"
