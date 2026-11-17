import httpx

from paw_server.modules.chat.llm import Completion, OpenAICompatLLM


def test_completion_records_returned_model_and_usage_and_preserves_text_interface():
    def respond(request):
        return httpx.Response(
            200,
            json={
                "model": "returned-model-version",
                "choices": [{"message": {"content": " Summary. "}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 7},
            },
        )

    llm = OpenAICompatLLM(
        "https://model.test", "test", "requested-model", 5, httpx.MockTransport(respond)
    )
    messages = [{"role": "user", "content": "summarize"}]
    assert llm.complete_with_usage(messages, 100) == Completion(
        "Summary.", "returned-model-version", 123, 7
    )
    assert llm.complete(messages, 100) == "Summary."
