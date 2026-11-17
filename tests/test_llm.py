import json
import threading
import time

import pytest
from pydantic import BaseModel

from sheetir.config import Settings
from sheetir.llm import LLM, HTTPStatusError, LLMError, check_schema, get_llm, parse_json

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["route", "n"],
          "properties": {"route": {"type": "string", "enum": ["data", "structure"]}, "n": {"type": "integer"}}}


class Reply(BaseModel):
    answer: str
    numbers: list[float] = []


class FakeServer:
    """A transport that answers from a script: a string is the message content, an exception is raised."""

    def __init__(self, *script):
        self.script, self.bodies = list(script), []

    def __call__(self, url, body, headers, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"choices": [{"message": {"content": item}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def make(server, **kw):
    kw.setdefault("retries", 2)
    return LLM("m", "http://127.0.0.1:8000/v1", transport=server, **kw)


# ---- parsing -------------------------------------------------------------------------------------------------------

def test_parse_json_takes_the_last_object_after_thinking_and_prose():
    text = '<think>maybe {"route": "x"}</think>Sure! First {"a": 1} then:\n```json\n{"route": "data", "n": 2}\n```'
    assert parse_json(text) == {"route": "data", "n": 2}
    assert parse_json('{"a": {"b": 1}} trailing') == {"a": {"b": 1}}


def test_check_schema_names_the_problem():
    check_schema({"route": "data", "n": 3}, SCHEMA)
    for bad, msg in [({"route": "data"}, "missing required field 'n'"), ({"route": "x", "n": 1}, "not one of"),
                     ({"route": "data", "n": True}, "expected integer"), ({"route": "data", "n": 1, "z": 0}, "unexpected")]:
        with pytest.raises(ValueError, match=msg):
            check_schema(bad, SCHEMA)


# ---- requests ------------------------------------------------------------------------------------------------------

def test_structured_output_request_and_usage():
    srv = FakeServer('{"route": "data", "n": 1}')
    llm = make(srv)
    assert llm.json("sys", "q", SCHEMA, "plan") == {"route": "data", "n": 1}
    body = srv.bodies[0]
    assert body["response_format"]["json_schema"]["name"] == "plan" and body["response_format"]["json_schema"]["strict"]
    assert body["temperature"] == 0 and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert llm.usage == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 2}


def test_invalid_reply_is_sent_back_with_the_error():
    srv = FakeServer('{"route": "data"}', '{"route": "data", "n": 4}')
    assert make(srv).json("sys", "q", SCHEMA) == {"route": "data", "n": 4}
    retry = srv.bodies[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"route": "data"}'}
    assert "missing required field 'n'" in retry[-1]["content"]


def test_gives_up_after_the_retries():
    srv = FakeServer("no json", "still none")
    with pytest.raises(LLMError, match="after 2 attempt"):
        make(srv, retries=1).json("sys", "q", SCHEMA)


def test_pydantic_schema_returns_a_model():
    srv = FakeServer('{"answer": "ok", "numbers": "x"}', '{"answer": "ok", "numbers": [1, 2.5]}')
    r = make(srv).chat_json([{"role": "user", "content": "q"}], Reply)
    assert isinstance(r, Reply) and r.numbers == [1, 2.5]
    assert srv.bodies[0]["response_format"]["json_schema"]["strict"] is False
    assert "numbers" in srv.bodies[1]["messages"][-1]["content"]


def test_prompt_mode_puts_the_schema_in_the_prompt():
    srv = FakeServer('Here you go: {"route": "structure", "n": 0}')
    assert make(srv, structured_output=False).json("sys", "q", SCHEMA) == {"route": "structure", "n": 0}
    body = srv.bodies[0]
    assert "response_format" not in body and '"enum": ["data", "structure"]' in body["messages"][-1]["content"]


def test_falls_back_to_prompt_mode_when_structured_output_is_rejected():
    srv = FakeServer(HTTPStatusError(400, "response_format json_schema is not supported", "x"),
                     '{"route": "data", "n": 1}', '{"route": "data", "n": 2}')
    llm = make(srv)
    assert llm.json("s", "q", SCHEMA)["n"] == 1 and llm.structured_output is False
    assert llm.json("s", "q", SCHEMA)["n"] == 2 and "response_format" not in srv.bodies[2]


def test_other_http_errors_are_raised(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    srv = FakeServer(HTTPStatusError(503, "busy", "x"), '{"route": "data", "n": 1}')
    assert make(srv).json("s", "q", SCHEMA)["n"] == 1  # transient: retried
    with pytest.raises(HTTPStatusError, match="HTTP 401"):
        make(FakeServer(HTTPStatusError(401, "bad key", "x"))).json("s", "q", SCHEMA)


@pytest.mark.parametrize("param,expected", [
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": True}}),
    ("think", {"think": True}), ("none", {}),
])
def test_thinking_switch(param, expected):
    srv = FakeServer("<think>hmm</think>hello")
    assert make(srv, thinking_param=param).chat([{"role": "user", "content": "hi"}], thinking=True) == "hello"
    body = srv.bodies[0]
    assert {k: body[k] for k in ("chat_template_kwargs", "think") if k in body} == expected


def test_think_off_also_sets_reasoning_effort():
    """Ollama's /v1 endpoint ignores `think`; reasoning_effort "none" is what turns thinking off there."""
    srv = FakeServer("hello")
    make(srv, thinking_param="think").chat([{"role": "user", "content": "hi"}], thinking=False)
    assert srv.bodies[0]["think"] is False and srv.bodies[0]["reasoning_effort"] == "none"


def test_openai_gets_its_own_parameters():
    srv = FakeServer("hi")
    llm = LLM("gpt", "https://api.openai.com/v1", "k", transport=srv)
    llm.chat([{"role": "user", "content": "q"}], max_tokens=50)
    body = srv.bodies[0]
    assert "temperature" not in body and body["max_completion_tokens"] == 50 and "chat_template_kwargs" not in body


def test_calls_are_logged(tmp_path):
    log = tmp_path / "logs" / "llm.jsonl"
    llm = make(FakeServer('{"route": "data", "n": 1}', HTTPStatusError(401, "bad", "x")), log_path=log)
    llm.json("sys", "question?", SCHEMA, "plan")
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "x"}])
    recs = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["name"] == "plan" and recs[0]["messages"][1]["content"] == "question?" and recs[0]["error"] is None
    assert recs[0]["usage"]["prompt_tokens"] == 10 and recs[0]["response_format"] is True
    assert "401" in recs[1]["error"]


def test_map_keeps_order_and_caps_concurrency():
    active, peak, lock = 0, 0, threading.Lock()

    def transport(url, body, headers, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"choices": [{"message": {"content": body["messages"][0]["content"]}}]}

    llm = LLM("m", "http://127.0.0.1:8000/v1", transport=transport, max_concurrency=3)
    out = llm.map(lambda i: llm.chat([{"role": "user", "content": str(i)}]), range(12))
    assert out == [str(i) for i in range(12)] and peak <= 3


def test_get_llm_follows_the_settings():
    assert get_llm(Settings()) is None
    assert get_llm(Settings(llm={"enabled": True, "base_url": "https://api.openai.com/v1"})) is None
    llm = get_llm(Settings(llm={"enabled": True, "model": "q", "max_concurrency": 2}))
    assert llm.model == "q" and llm.max_concurrency == 2 and llm.log_path.name == "llm.jsonl"
