from __future__ import annotations

import copy
import json

import pytest

from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.miora.tools import MioraToolRequestError, _matches, parse_response, prepare_prompt


def payload(**extra):
    return {
        "messages": [{"role": "user", "content": "Read the source file"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {"type": "function", "function": {"name": "search", "parameters": {}}},
        ],
        **extra,
    }


def envelope(calls=None, content=None):
    return json.dumps({"content": content, "tool_calls": calls or []})


def call(name="read_file", **arguments):
    return {"name": name, "arguments": arguments}


def test_prompt_history_no_execution_or_mutation():
    request = payload(parallel_tool_calls=False)
    request["messages"] = [
        {"role": "system", "content": "Act as coding assistant"},
        {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "t", "content": "File contents"},
        {"role": "user", "content": "Explain the file"},
    ]
    original = copy.deepcopy(request)
    result = prepare_prompt(request)
    assert result[0]["type"] == "text"
    text = result[0]["text"]
    assert "Do not execute any" in text
    assert "at most one tool" in text
    assert "File contents" in text and '"role": "system"' in text
    assert '"tool_call_id": "t"' in text and "read_file" in text
    assert request == original
    assert "Client functions" not in prepare_prompt({"messages": []})[0]["text"]
    assert "Conversation JSON" in prepare_prompt({})[0]["text"]
    assert "at most one tool" not in prepare_prompt(payload())[0]["text"]


def test_output_budget():
    assert "within 50 tokens" in prepare_prompt({"max_tokens": 50})[0]["text"]
    assert (
        "within 20 tokens"
        in prepare_prompt({"max_completion_tokens": 20, "max_tokens": 50})[0]["text"]
    )
    assert "output budget" not in prepare_prompt({"max_tokens": None})[0]["text"]
    for budget in (0, -1, True, "5", 5.0):
        with pytest.raises(MioraToolRequestError, match="正整数"):
            prepare_prompt({"max_tokens": budget})


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e10000"])
def test_tool_response_rejects_non_finite_numbers_even_without_argument_schema(number):
    response = ('{"content":null,"tool_calls":[{"name":"search",'
                '"arguments":{"nested":[{"x":' + number + '}]}}]}')
    with pytest.raises(UpstreamProtocolViolation):
        parse_response(response, payload())


@pytest.mark.parametrize("constraint,value,accepted", [
    ({"enum": [1]}, True, False), ({"enum": [True]}, 1, False),
    ({"const": False}, True, False), ({"enum": [True]}, True, True),
    ({"enum": [1]}, 1.0, True), ({"const": 1.0}, 1, True),
    ({"enum": [{"x": True}]}, {"x": 1}, False),
    ({"const": {"x": [True]}}, {"x": [1]}, False),
    ({"enum": [{"x": [1]}]}, {"x": [1.0]}, True),
    ({"enum": [{}]}, {}, True), ({"enum": [{}]}, [], False),
    ({"enum": [{"x": 1}]}, {"y": 1}, False),
    ({"enum": [[1]]}, [1, 2], False), ({"enum": [[1]]}, "1", False),
    ({"const": []}, [], True),
])
def test_enum_and_const_follow_json_equality_not_python_bool_number_equality(
        constraint, value, accepted):
    request = payload()
    request["tools"][1]["function"]["parameters"] = {"properties": {"x": constraint}}
    response = envelope([call("search", x=value)])
    if accepted:
        events = parse_response(response, request)
        assert json.loads(events[0].tool_calls[0]["function"]["arguments"])["x"] == value
    else:
        with pytest.raises(UpstreamProtocolViolation):
            parse_response(response, request)


@pytest.mark.parametrize(
    "tools",
    [
        None,
        {},
        [None],
        [{"type": "web_search"}],
        [{"type": "function", "function": None}],
        [{"type": "other", "function": {"name": "t"}}],
        [{"type": "function", "function": {"name": ""}}],
        [{"type": "function", "function": {"name": 1}}],
        [{"type": "function", "function": {"name": "   "}}],
        [{"type": "function", "function": {"name": "t", "parameters": []}}],
    ],
)
def test_invalid_definitions(tools):
    with pytest.raises(MioraToolRequestError) as error:
        prepare_prompt({"tools": tools})
    assert error.value.kind() == ErrKind.INVALID


def test_duplicate_tools_and_default_parameters():
    definition = {"type": "function", "function": {"name": "t"}}
    with pytest.raises(MioraToolRequestError, match="不重复"):
        prepare_prompt({"tools": [definition, definition]})
    result = parse_response(envelope([call("t", x=1)]), {"tools": [definition]})
    assert result[0].tool_calls[0]["function"]["name"] == "t"


@pytest.mark.parametrize(
    "choice",
    [
        None,
        [],
        "invalid",
        {"type": "other"},
        {"type": "function"},
        {"type": "function", "function": None},
        {"type": "function", "function": {"name": 1}},
        {"type": "function", "function": {"name": "unknown"}},
    ],
)
def test_invalid_choice(choice):
    with pytest.raises(MioraToolRequestError):
        prepare_prompt(payload(tool_choice=choice))


def test_required_needs_definitions_and_messages_shape():
    with pytest.raises(MioraToolRequestError, match="至少一个"):
        prepare_prompt({"tool_choice": "required"})
    with pytest.raises(MioraToolRequestError, match="对话列表"):
        prepare_prompt({"messages": "text"})


def test_calls_text_and_fenced_json():
    events = parse_response(envelope([call(path="main.py")], "Need to read source"), payload())
    assert events[0].kind == EventKind.CONTENT
    assert events[1].kind == EventKind.TOOL_CALLS
    tool = events[1].tool_calls[0]
    assert tool["index"] == 0 and tool["id"].startswith("call_")
    assert tool["type"] == "function"
    assert json.loads(tool["function"]["arguments"]) == {"path": "main.py"}
    assert (
        parse_response("```json\n" + envelope(content="text") + "\n```", payload())[0].content
        == "text"
    )
    assert (
        parse_response("```\n" + envelope(content="text") + "\n```", payload())[0].content == "text"
    )
    assert parse_response(envelope(), payload()) == []
    assert parse_response("plain text", {})[0].content == "plain text"
    assert parse_response("", {}) == []
    calls = parse_response(envelope([call(path="a"), call(path="b")]), payload())[0].tool_calls
    assert [item["index"] for item in calls] == [0, 1]
    assert calls[0]["id"] != calls[1]["id"]


@pytest.mark.parametrize(
    "text",
    [
        "bad secret output",
        "[]",
        "null",
        "{}",
        '{"content":1,"tool_calls":[]}',
        '{"content":null,"tool_calls":{}}',
        '{"content":null,"tool_calls":[],"extra":1}',
        envelope([None]),
        envelope([{}]),
        envelope([{**call(path="x"), "extra": 1}]),
        envelope([{"name": 1, "arguments": {}}]),
        envelope([call("unknown")]),
        envelope([{"name": "search", "arguments": "{}"}]),
        envelope([call()]),
        envelope([call(path=3)]),
        envelope([call(path="x", unknown=True)]),
    ],
)
def test_invalid_output(text):
    with pytest.raises(UpstreamProtocolViolation) as error:
        parse_response(text, payload())
    assert "secret" not in str(error.value)


def test_choices_and_parallel_limit():
    text = envelope([call(path="main.py")])
    assert parse_response(text, payload(tool_choice="required"))[0].kind == EventKind.TOOL_CALLS
    assert (
        parse_response(envelope(content="answer"), payload(tool_choice="none"))[0].content
        == "answer"
    )
    forced = {"type": "function", "function": {"name": "read_file"}}
    assert 'forced_function="read_file"' in prepare_prompt(payload(tool_choice=forced))[0]["text"]
    assert parse_response(text, payload(tool_choice=forced))[0].kind == EventKind.TOOL_CALLS
    for body, values in (
        (text, {"tool_choice": "none"}),
        (envelope(), {"tool_choice": "required"}),
        (envelope(), {"tool_choice": forced}),
        (envelope([call("search")]), {"tool_choice": forced}),
        (envelope([call(path="a"), call(path="b")]), {"parallel_tool_calls": False}),
    ):
        with pytest.raises(UpstreamProtocolViolation):
            parse_response(body, payload(**values))


@pytest.mark.parametrize(
    ("value", "schema", "expected"),
    [
        ({}, True, True),
        ({}, False, False),
        ({}, None, False),
        ({}, {"type": "object"}, True),
        ([], {"type": "array"}, True),
        ("s", {"type": "string"}, True),
        (True, {"type": "boolean"}, True),
        (None, {"type": "null"}, True),
        (1.0, {"type": "integer"}, True),
        (1.5, {"type": "integer"}, False),
        (1.5, {"type": "number"}, True),
        (10**500, {"type": "integer"}, True),
        (True, {"type": "integer"}, False),
        (float("inf"), {"type": "number"}, False),
        ("s", {"type": "unknown"}, False),
        ("s", {"type": 5}, False),
        (None, {"type": ["string", "null"]}, True),
        ("a", {"enum": ["a", "b"]}, True),
        ("c", {"enum": ["a", "b"]}, False),
        ("a", {"const": "a"}, True),
        ("b", {"const": "a"}, False),
        (1, {"allOf": [{"type": "integer"}, {"minimum": 0}]}, True),
        (-1, {"allOf": [{"type": "integer"}, {"minimum": 0}]}, False),
        (None, {"anyOf": [{"type": "null"}, {"type": "string"}]}, True),
        (3, {"anyOf": [{"type": "null"}, {"type": "string"}]}, False),
        (3, {"oneOf": [{"type": "integer"}, {"type": "string"}]}, True),
        (3, {"oneOf": [{"type": "integer"}, {"type": "number"}]}, False),
        ({"a": "s"}, {"required": ["a"], "properties": {"a": {"type": "string"}}}, True),
        ({}, {"required": ["a"]}, False),
        ({"b": 2}, {"properties": {}, "additionalProperties": False}, False),
        ([1, 2], {"items": {"type": "integer"}, "minItems": 1, "maxItems": 2}, True),
        ([], {"minItems": 1}, False),
        ([1, 2], {"maxItems": 1}, False),
        (["s"], {"items": {"type": "integer"}}, False),
        ("ss", {"minLength": 1, "maxLength": 2}, True),
        ("", {"minLength": 1}, False),
        ("ss", {"maxLength": 1}, False),
        (2, {"minimum": 1, "maximum": 2}, True),
        (0, {"minimum": 1}, False),
        (3, {"maximum": 2}, False),
        (float("nan"), {}, False),
        (False, {}, True),
        (None, {}, True),
    ],
)
def test_function_schema(value, schema, expected):
    assert _matches(value, schema, schema) == expected


def test_local_refs_depth_and_malformed_schema():
    schema = {
        "$defs": {"p/~": {"type": "string"}},
        "properties": {"path": {"$ref": "#/$defs/p~1~0"}},
        "required": ["path"],
    }
    assert _matches({"path": "x"}, schema, schema)
    assert not _matches({"path": 2}, schema, schema)
    assert not _matches({}, {"$ref": "https://external.invalid/schema"}, {})
    assert not _matches({}, {"$ref": 3}, {})
    assert not _matches({}, {"$ref": "#/missing"}, {})
    assert not _matches({}, {"$ref": "#/a/b"}, {"a": "not-object"})
    circular = {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"}
    assert not _matches({}, circular, circular)
    for malformed in (
        {"properties": None},
        {"type": "object", "required": 2},
        {"properties": {"path": {"minLength": "bad"}}},
    ):
        request = payload()
        request["tools"][0]["function"]["parameters"] = malformed
        with pytest.raises(MioraToolRequestError):
            parse_response(envelope([call(path="x")]), request)


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": None},
        {"properties": {1: {}}},
        {"properties": {"path": None}},
        {"definitions": []},
        {"$defs": {"nested": []}},
        {"required": None},
        {"required": [1]},
        {"required": ["x", "x"]},
        {"items": []},
        {"additionalProperties": None},
        {"enum": None},
        {"enum": []},
        {"allOf": {}},
        {"anyOf": []},
        {"oneOf": [None]},
        {"type": []},
        {"type": None},
        {"type": ["string", 3]},
        {"type": "not-json-type"},
        {"minimum": "bad"},
        {"maximum": float("inf")},
        {"minItems": -1},
        {"maxLength": 0.5},
        {"$ref": None},
        {"$ref": "https://external.invalid/schema"},
        {"$ref": "#/missing"},
        {"$ref": "#/x/y", "x": "not-object"},
        {"$ref": "#/schemas/invalid", "schemas": {"invalid": {"required": None}}},
    ],
)
def test_bad_schema_is_request_error_before_prompt(schema):
    request = payload()
    request["tools"][0]["function"]["parameters"] = schema
    with pytest.raises(MioraToolRequestError) as error:
        prepare_prompt(request)
    assert error.value.kind() == ErrKind.INVALID


def test_schema_definitions_refs_and_unknown_keywords():
    request = payload()
    request["tools"][0]["function"]["parameters"] = {
        "type": ["object", "null"],
        "enum": [{"path": "x"}, None],
        "required": [],
        "properties": {"path": {"$ref": "#/$defs/path"}},
        "$defs": {"path": {"type": "string", "minLength": 0, "maxLength": 100}},
        "definitions": {"unused": True},
        "items": False,
        "additionalProperties": False,
        "anyOf": [True, False],
        "oneOf": [{"type": "object"}, {"type": "null"}],
        "allOf": [True],
        "minimum": -1,
        "maximum": 3,
        "minItems": 0,
        "maxItems": 2,
        "unknown-extension": {"anything": "is preserved"},
    }
    assert "unknown-extension" in prepare_prompt(request)[0]["text"]
    request["tools"][0]["function"]["parameters"] = {
        "$defs": {"recursive": {"$ref": "#/$defs/recursive"}},
        "$ref": "#/$defs/recursive",
    }
    assert prepare_prompt(request)[0]["type"] == "text"
    request["tools"][0]["function"]["parameters"] = {
        "$defs": {"closed": False},
        "$ref": "#/$defs/closed",
    }
    assert prepare_prompt(request)[0]["type"] == "text"
    deep: dict = {}
    for _ in range(34):
        deep = {"properties": {"nested": deep}}
    request["tools"][0]["function"]["parameters"] = deep
    with pytest.raises(MioraToolRequestError, match="32"):
        prepare_prompt(request)
