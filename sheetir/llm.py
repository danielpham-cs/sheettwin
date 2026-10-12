"""The language model behind SheetIR's understanding features, behind one small interface.

Everything here goes through `LLM.json(system, user, schema)`, which returns an object matching a JSON schema. The only
implementation today is OpenAI's Chat Completions API; a local model (llama.cpp, OpenVINO on the project's workstation)
can replace it without touching the callers.

Configuration (environment variables, or a git-ignored `.env` file in the working directory):
    OPENAI_API_KEY       enables the LLM features
    SHEETIR_LLM_MODEL    model name (default gpt-5.4-mini)
    OPENAI_BASE_URL      another OpenAI-compatible endpoint, e.g. a local llama.cpp or vLLM server

What is sent: a compact sketch of a workbook (sheetir.sketch) and the user's question, never the file itself.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gpt-5.4-mini"
TIMEOUT = 120
RETRIES = 2


class LLMError(Exception):
    pass


def _dotenv() -> dict[str, str]:
    for d in (Path.cwd(), Path(__file__).resolve().parents[1]):
        p = d / ".env"
        if p.is_file():
            out = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
            return out
    return {}


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _dotenv().get(name) or default


class LLM:
    """A chat model that answers in JSON matching a schema."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = "https://api.openai.com/v1"):
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip("/")
        self.provider = "OpenAI" if "api.openai.com" in self.base_url else self.base_url
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def json(self, system: str, user: str, schema: dict, name: str = "answer") -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        }
        data = json.dumps(body).encode("utf-8")
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    res = json.load(r)
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:400].decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503) and attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(f"{self.provider} returned HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(f"cannot reach {self.provider}: {e}") from e
        u = res.get("usage", {})
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += u.get("completion_tokens", 0)
        msg = res["choices"][0]["message"]
        if msg.get("refusal"):
            raise LLMError(f"the model refused: {msg['refusal']}")
        try:
            return json.loads(msg["content"])
        except (TypeError, json.JSONDecodeError) as e:
            raise LLMError("the model did not return JSON") from e


def get_llm() -> LLM | None:
    """The configured model, or None when no API key is set (LLM features are then off)."""
    key = setting("OPENAI_API_KEY")
    if not key:
        return None
    return LLM(key, setting("SHEETIR_LLM_MODEL", DEFAULT_MODEL), setting("OPENAI_BASE_URL", "https://api.openai.com/v1"))
