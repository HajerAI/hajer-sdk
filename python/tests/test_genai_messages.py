"""Every provider's message shape becomes the one shape a span carries: roles folded, parts typed, nothing dropped."""

from __future__ import annotations

import json

import pytest

from hajer._genai_messages import input_messages, output_messages, system_instructions
from hajer._json import JsonObject, JsonValue


class TestInput:
    def test_openai_chat_text_tool_calls_and_tool_results(self) -> None:
        messages: list[JsonValue] = [
            {"role": "developer", "content": "Be terse."},
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"id": 1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "shipped"},
        ]
        assert input_messages("chat.completions", messages) == [
            {"role": "system", "parts": [{"type": "text", "content": "Be terse."}]},
            {"role": "user", "parts": [{"type": "text", "content": "hi"}, {"type": "image_url"}]},
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "call-1", "name": "lookup", "arguments": {"id": 1}}],
            },
            {"role": "tool", "parts": [{"type": "tool_call_response", "id": "call-1", "result": "shipped"}]},
        ]

    def test_unparseable_tool_arguments_stay_a_string(self) -> None:
        messages: list[JsonValue] = [
            {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{not json"}}]}
        ]
        (message,) = input_messages("chat.completions", messages)
        assert message["parts"] == [{"type": "tool_call", "id": "c", "name": "f", "arguments": "{not json"}]

    def test_responses_items(self) -> None:
        items: list[JsonValue] = [
            {"role": "user", "content": [{"type": "input_text", "text": "send it"}]},
            {"type": "function_call", "call_id": "fc-1", "name": "send", "arguments": '{"to": "x"}'},
            {"type": "function_call_output", "call_id": "fc-1", "output": "sent"},
        ]
        assert input_messages("responses", items) == [
            {"role": "user", "parts": [{"type": "text", "content": "send it"}]},
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "fc-1", "name": "send", "arguments": {"to": "x"}}],
            },
            {"role": "tool", "parts": [{"type": "tool_call_response", "id": "fc-1", "result": "sent"}]},
        ]

    def test_a_string_input_is_one_user_message(self) -> None:
        assert input_messages("responses", "hi") == [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]

    def test_anthropic_blocks(self) -> None:
        messages: list[JsonValue] = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "tu-1", "name": "lookup", "input": {"id": 1}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-1", "content": [{"type": "text", "text": "shipped"}]}
                ],
            },
        ]
        assert input_messages("messages", messages) == [
            {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "content": "checking"},
                    {"type": "tool_call", "id": "tu-1", "name": "lookup", "arguments": {"id": 1}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {"type": "tool_call_response", "id": "tu-1", "result": [{"type": "text", "text": "shipped"}]}
                ],
            },
        ]

    def test_genai_contents(self) -> None:
        contents: list[JsonValue] = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"function_call": {"id": "fc", "name": "lookup", "args": {"id": 1}}}]},
            {
                "role": "user",
                "parts": [{"function_response": {"id": "fc", "name": "lookup", "response": {"ok": True}}}],
            },
        ]
        assert input_messages("genai.generate_content", contents) == [
            {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "fc", "name": "lookup", "arguments": {"id": 1}}],
            },
            {"role": "user", "parts": [{"type": "tool_call_response", "id": "fc", "result": {"ok": True}}]},
        ]

    @pytest.mark.parametrize("item", [7, ["a", "b"], {"content": "no role"}, {"role": "narrator", "content": "aside"}])
    def test_an_unknown_shape_is_kept_as_text_never_dropped(self, item: JsonValue) -> None:
        (message,) = input_messages("chat.completions", [item])
        assert message["parts"]
        assert item == 7 or json.dumps(message)  # nothing raised, something carried
        if isinstance(item, dict) and item.get("role") == "narrator":
            assert message["role"] == "narrator", "an unknown role is kept, not guessed"

    def test_nothing_is_nothing(self) -> None:
        assert input_messages("chat.completions", None) == []


class TestOutput:
    def test_one_assistant_message_per_choice_with_the_tool_calls_on_the_first(self) -> None:
        answers = output_messages("chat.completions", ["first", "second"], None, [("c", "f", '{"a": 1}')], "stop")
        assert answers == [
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "content": "first"},
                    {"type": "tool_call", "id": "c", "name": "f", "arguments": {"a": 1}},
                ],
                "finish_reason": "stop",
            },
            {"role": "assistant", "parts": [{"type": "text", "content": "second"}], "finish_reason": "stop"},
        ]

    def test_a_streamed_answer_is_its_assembled_text(self) -> None:
        assert output_messages("messages", None, "hello", [], None) == [
            {"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}
        ]

    def test_only_tool_calls_is_still_one_message(self) -> None:
        assert output_messages("messages", [], None, [("tu", "lookup", {"id": 1})], "tool_use") == [
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "tu", "name": "lookup", "arguments": {"id": 1}}],
                "finish_reason": "tool_use",
            }
        ]

    def test_responses_blocks_become_text_parts(self) -> None:
        blocks: list[JsonValue] = [[{"type": "output_text", "text": "done", "annotations": []}]]
        assert output_messages("responses", blocks, None, [], "completed") == [
            {"role": "assistant", "parts": [{"type": "text", "content": "done"}], "finish_reason": "completed"}
        ]

    def test_no_answer_is_no_message(self) -> None:
        assert output_messages("chat.completions", None, None, [], None) == []


class TestSystemInstructions:
    def test_a_string_and_a_list_of_blocks(self) -> None:
        assert system_instructions("Be terse.") == [{"type": "text", "content": "Be terse."}]
        blocks: JsonObject = {"text": "unused"}
        del blocks
        assert system_instructions([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == [
            {"type": "text", "content": "a"},
            {"type": "text", "content": "b"},
        ]

    def test_none_and_nothing(self) -> None:
        assert system_instructions(None) is None
        assert system_instructions([]) is None
