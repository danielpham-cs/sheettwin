"""The language model behind SheetIR's understanding features (docs/excel-agent-system-design.md §8.1).

One client for any OpenAI-compatible Chat Completions endpoint: a local llama.cpp, vLLM, OpenVINO or Ollama server by
default, a cloud API only when the settings allow it (sheetir.config). It is synchronous; `map` runs calls in parallel
threads, and a semaphore caps the requests in flight at llm.max_concurrency across all threads.

    chat(messages)                  -> str
    chat_json(messages, schema)     -> a pydantic model (schema is a BaseModel class) or a dict (schema is a JSON schema)
    json(system, user, schema)      -> dict, the form the Q&A code uses

chat_json asks for structured output (response_format json_schema) when llm.structured_output is on, and turns it off
for the rest of the session if the backend rejects it. Without it, the schema goes into the prompt and the last JSON
object in the reply is taken. Either way the object is validated against the schema; a reply that does not parse or
validate is sent back with the error, up to llm.retries times. Reasoning blocks (<think>…</think>) are dropped.

Thinking is off unless asked for. How it is switched depends on the server (llm.thinking_param): chat_template_kwargs
{"enable_thinking": …} for vLLM, SGLang and llama.cpp with Qwen templates, `think` for Ollama, nothing for OpenAI.

Every call is appended to <paths.log_dir>/llm.jsonl (messages, output, latency, tokens) when llm.log is on. The log
holds workbook contents: it stays on this machine and is git-ignored.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings, get_settings, llm_block_reason

T = TypeVar("T")
R = TypeVar("R")

_TRANSIENT = (408, 409, 429, 500, 502, 503, 504)
_THINK = re.compile(r"<think>.*?</think>|<think>.*\Z", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class LLMError(Exception):
    pass


class HTTPStatusError(LLMError):
    def __init__(self, code: int, detail: str, provider: str):
        super().__init__(f"{provider} returned HTTP {code}: {detail}")
        self.code, self.detail = code, detail


class InvalidReply(ValueError):
    """The reply did not parse as JSON or did not match the schema."""


# ---- transport -------------------------------------------------------------------------------------------------------

Transport = Callable[[str, dict, dict, float], dict]


def urllib_transport(url: str, body: dict, headers: dict, timeout: float) -> dict:
    """POST `body` as JSON and return the decoded JSON reply. HTTP errors raise HTTPStatusError(code, detail)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPStatusError(e.code, e.read()[:400].decode("utf-8", "replace"), url) from e


# ---- JSON from a reply -----------------------------------------------------------------------------------------------

def strip_thinking(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def parse_json(text: str) -> Any:
    """The JSON object in a model reply: the whole reply, else the last top-level {...} in it (models often wrap the
    object in prose or a code fence)."""
    text = _FENCE.sub("", strip_thinking(text))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    dec, last, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            last = obj
        i = text.find("{", end)
    if last is None:
        raise InvalidReply("the reply holds no JSON object")
    return last


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def check_schema(value: Any, schema: dict, path: str = "$") -> None:
    """Validate `value` against the subset of JSON Schema the prompts use (type, properties, required,
    additionalProperties false, items, enum). Raises InvalidReply naming the first problem."""
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        ok = False
        for t in types:
            if t == "integer":
                ok |= isinstance(value, int) and not isinstance(value, bool)
            elif t == "number":
                ok |= isinstance(value, (int, float)) and not isinstance(value, bool)
            elif t in _TYPES:
                ok |= isinstance(value, _TYPES[t]) and not (t != "boolean" and isinstance(value, bool))
        if not ok:
            raise InvalidReply(f"{path}: expected {' or '.join(types)}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidReply(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise InvalidReply(f"{path}: missing required field {k!r}")
        if schema.get("additionalProperties") is False:
            extra = [k for k in value if k not in props]
            if extra:
                raise InvalidReply(f"{path}: unexpected field(s) {', '.join(map(repr, extra))}")
        for k, v in value.items():
            if k in props:
                check_schema(v, props[k], f"{path}.{k}")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            check_schema(v, schema["items"], f"{path}[{i}]")


# ---- client ----------------------------------------------------------------------------------------------------------

class LLM:
    """A chat model on an OpenAI-compatible endpoint."""

    def __init__(self, model: str, base_url: str, api_key: str | None = None, *, timeout: float = 120,
                 retries: int = 2, structured_output: bool = True, thinking_param: str = "auto",
                 max_concurrency: int = 8, log_path: Path | str | None = None, transport: Transport | None = None):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.timeout, self.retries = timeout, retries
        self.structured_output = structured_output
        self.is_openai = "api.openai.com" in self.base_url
        self.provider = "OpenAI" if self.is_openai else self.base_url
        if thinking_param == "auto":
            thinking_param = "none" if self.is_openai else "chat_template_kwargs"
        self.thinking_param = thinking_param
        self.max_concurrency = max_concurrency
        self.log_path = Path(log_path) if log_path else None
        self.transport = transport or urllib_transport
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s: Settings, **kw) -> "LLM":
        c = s.llm
        log = Path(s.paths.log_dir) / "llm.jsonl" if c.log else None
        return cls(c.model, c.base_url, c.api_key, timeout=c.timeout_s, retries=c.retries,
                   structured_output=c.structured_output, thinking_param=c.thinking_param,
                   max_concurrency=c.max_concurrency, log_path=log, **kw)

    # -- one request ---------------------------------------------------------------------------------------------------

    def _thinking(self, on: bool) -> dict:
        if self.thinking_param == "chat_template_kwargs":
            return {"chat_template_kwargs": {"enable_thinking": on}}
        if self.thinking_param == "think":
            # Ollama ignores `think` on its OpenAI-compatible /v1 endpoint; there, reasoning_effort "none" is what
            # turns thinking off. /api/chat honours `think`, so both are sent.
            return {"think": True} if on else {"think": False, "reasoning_effort": "none"}
        return {}

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self.retries + 1):
            try:
                with self._slots:
                    return self.transport(url, body, headers, self.timeout)
            except HTTPStatusError as e:
                if e.code in _TRANSIENT and attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise HTTPStatusError(e.code, e.detail, self.provider) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(f"cannot reach {self.provider}: {e}") from e
        raise AssertionError("unreachable")

    def _complete(self, messages: list[dict], *, name: str, temperature: float, max_tokens: int | None,
                  thinking: bool, response_format: dict | None = None) -> str:
        body: dict = {"model": self.model, "messages": messages, **self._thinking(thinking)}
        if self.is_openai:
            # OpenAI's reasoning models accept only the default temperature, and take max_completion_tokens
            if max_tokens:
                body["max_completion_tokens"] = max_tokens
        else:
            body["temperature"] = temperature
            if max_tokens:
                body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format
        t0, res, err = time.perf_counter(), None, None
        try:
            res = self._post(body)
            msg = res["choices"][0]["message"]
            if msg.get("refusal"):
                raise LLMError(f"the model refused: {msg['refusal']}")
            return strip_thinking(msg.get("content") or "")
        except (KeyError, IndexError, TypeError) as e:
            err = LLMError(f"{self.provider} returned an unexpected reply: {str(res)[:200]}")
            raise err from e
        except LLMError as e:
            err = e
            raise
        finally:
            u = (res or {}).get("usage") or {}
            with self._lock:
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                self.usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
            self._log(name, body, res, err, time.perf_counter() - t0)

    def _log(self, name: str, body: dict, res: dict | None, err: Exception | None, latency: float) -> None:
        if not self.log_path:
            return
        out = None
        if res:
            try:
                out = res["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError, AttributeError):
                out = None
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "name": name,
               "model": self.model, "endpoint": self.provider, "messages": body["messages"],
               "response_format": "response_format" in body, "output": out, "latency_s": round(latency, 3),
               "usage": (res or {}).get("usage"), "error": f"{type(err).__name__}: {err}" if err else None}
        try:
            with self._lock:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # logging must never break an answer

    # -- public API ----------------------------------------------------------------------------------------------------

    def chat(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = 1024,
             thinking: bool = False, name: str = "chat") -> str:
        return self._complete(messages, name=name, temperature=temperature, max_tokens=max_tokens, thinking=thinking)

    def chat_json(self, messages: list[dict], schema: type[BaseModel] | dict, *, name: str = "answer",
                  retries: int | None = None, temperature: float = 0.0, max_tokens: int | None = None,
                  thinking: bool = False) -> Any:
        """One JSON object matching `schema`: a validated instance when `schema` is a pydantic model, else a dict."""
        is_model = isinstance(schema, type) and issubclass(schema, BaseModel)
        json_schema = schema.model_json_schema() if is_model else schema
        retries = self.retries if retries is None else retries
        messages = list(messages)
        prompt_mode_messages = None
        attempt = 0
        while True:
            if self.structured_output:
                rf = {"type": "json_schema", "json_schema": {"name": name, "schema": json_schema, "strict": not is_model}}
                sent = messages
            else:
                rf = None
                if prompt_mode_messages is None:
                    prompt_mode_messages = _with_schema(messages, json_schema)
                sent = prompt_mode_messages
            try:
                text = self._complete(sent, name=name, temperature=temperature, max_tokens=max_tokens,
                                      thinking=thinking, response_format=rf)
            except HTTPStatusError as e:
                if self.structured_output and e.code in (400, 422) and re.search(
                        r"response_format|json_schema|structured|grammar", e.detail, re.I):
                    self.structured_output = False  # the backend does not do structured output: ask in the prompt
                    continue
                raise
            try:
                obj = parse_json(text)
                if is_model:
                    return schema.model_validate(obj)
                check_schema(obj, json_schema)
                return obj
            except (InvalidReply, ValidationError) as e:
                if attempt >= retries:
                    raise LLMError(f"the model did not return valid JSON for {name!r} after {attempt + 1} "
                                   f"attempt(s): {_short(e)}") from e
                attempt += 1
                fix = [{"role": "assistant", "content": text},
                       {"role": "user", "content": f"Your reply was not valid: {_short(e)}. Reply again with one JSON "
                                                   "object that matches the schema, and nothing else."}]
                if self.structured_output:
                    messages = messages + fix
                else:
                    prompt_mode_messages = prompt_mode_messages + fix

    def json(self, system: str, user: str, schema: dict | type[BaseModel], name: str = "answer") -> dict:
        """chat_json for one system and one user message, as a dict."""
        r = self.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], schema,
                           name=name)
        return r.model_dump() if isinstance(r, BaseModel) else r

    def map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        """fn over items in parallel threads, results in order. Requests in flight stay within max_concurrency."""
        items = list(items)
        if len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(items))) as ex:
            return list(ex.map(fn, items))


def _with_schema(messages: list[dict], schema: dict) -> list[dict]:
    """The messages with the schema appended to the last user message, for backends without structured output."""
    out = [dict(m) for m in messages]
    note = ("\n\nReply with one JSON object that matches this JSON schema, and nothing else:\n"
            + json.dumps(schema, ensure_ascii=False))
    for m in reversed(out):
        if m.get("role") == "user":
            m["content"] = f"{m.get('content') or ''}{note}"
            return out
    return out + [{"role": "user", "content": note.strip()}]


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        errs = e.errors()[:3]
        return "; ".join(f"{'.'.join(map(str, x['loc'])) or '$'}: {x['msg']}" for x in errs)
    return str(e)[:300]


def get_llm(settings: Settings | None = None) -> LLM | None:
    """The configured model, or None when it is off or not allowed (see llm_status for why)."""
    s = settings or get_settings()
    return None if llm_block_reason(s) else LLM.from_settings(s)


def llm_status(settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    reason = llm_block_reason(s)
    return {"enabled": reason is None, "model": None if reason else s.llm.model,
            "endpoint": None if reason else s.llm.base_url, "reason": reason}


LLMClient = LLM  # the name used in the design doc

__all__ = ["LLM", "LLMClient", "LLMError", "HTTPStatusError", "get_llm", "llm_status", "parse_json", "check_schema"]
