"""Settings (docs/excel-agent-system-design.md §10): config/settings.yaml, then environment overrides.

Where the file is read from, first found wins: $SHEETIR_CONFIG, ./config/settings.yaml, <repo>/config/settings.yaml.
No file is fine: every setting has a default here. The LLM block can also be set from the environment (or a git-ignored
.env file in the working directory), which is how Docker and earlier setups configure it:

    OPENAI_BASE_URL            llm.base_url (and turns the model on)
    OPENAI_API_KEY             llm.api_key  (and turns the model on)
    SHEETIR_LLM_MODEL          llm.model
    SHEETIR_LLM_THINKING_PARAM llm.thinking_param (think for Ollama)
    SHEETIR_ALLOW_REMOTE_LLM   llm.allow_remote (1/true/yes)
    SHEETIR_LOG_DIR            paths.log_dir

Offline by default (goal G5): an endpoint that is neither this machine nor the local network is refused unless
llm.allow_remote is set, since the workbook's contents would leave the network.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO = Path(__file__).resolve().parents[1]


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LLMSettings(_Section):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "qwen3.5-35b-a3b-int4"
    api_key: str | None = None
    allow_remote: bool = False
    max_concurrency: int = Field(8, ge=1)
    timeout_s: float = Field(120, gt=0)
    retries: int = Field(2, ge=0)
    structured_output: bool = True
    thinking_param: Literal["auto", "chat_template_kwargs", "think", "none"] = "auto"
    log: bool = True


class EmbeddingSettings(_Section):
    model_path: str = "models/embedding"
    hybrid_alpha: float = Field(0.6, ge=0, le=1)


class IngestSettings(_Section):
    max_file_mb: int = 50
    max_sheets: int = 300
    large_file_cells: int = 2_000_000
    recalc_threshold: int = 50
    libreoffice_bin: str = "soffice"


class DetectSettings(_Section):
    accept_threshold: float = 0.8
    gap_rows: int = 1
    gap_cols: int = 1
    llm_retries: int = 2
    max_tokens_per_block: int = 6000


class CleanSettings(_Section):
    ffill_vertical_merges: bool = True


class RelationSettings(_Section):
    family_jaccard: float = 0.9
    accept_score: float = 0.8
    llm_confirm_low: float = 0.5
    sample_distinct: int = 5000


class AgentSettings(_Section):
    max_steps: int = 8
    thinking: bool = False
    sql_timeout_s: float = 10
    max_rows_to_llm: int = 30
    max_rows_return: int = 200
    search_k: int = 8


class PathSettings(_Section):
    cache_dir: str = ".cache"
    log_dir: str = ".logs"


class Settings(_Section):
    offline: bool = True
    llm: LLMSettings = LLMSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    ingest: IngestSettings = IngestSettings()
    detect: DetectSettings = DetectSettings()
    clean: CleanSettings = CleanSettings()
    relations: RelationSettings = RelationSettings()
    agent: AgentSettings = AgentSettings()
    paths: PathSettings = PathSettings()
    source: str | None = None  # the file these settings were read from, if any


def config_path() -> Path | None:
    if os.environ.get("SHEETIR_CONFIG"):
        return Path(os.environ["SHEETIR_CONFIG"])
    for p in (Path.cwd() / "config" / "settings.yaml", REPO / "config" / "settings.yaml"):
        if p.is_file():
            return p
    return None


def dotenv(directory: Path | None = None) -> dict[str, str]:
    """KEY=value lines of the first .env found in `directory` (default: the working directory, then the repo)."""
    for d in ([directory] if directory else [Path.cwd(), REPO]):
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


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_settings(path: Path | str | None = None, env: dict[str, str] | None = None) -> Settings:
    """Settings from `path` (default: config_path()) with overrides from `env` (default: .env, then os.environ)."""
    path = Path(path) if path else config_path()
    data = {}
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"settings file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
    if env is None:
        env = {**dotenv(), **{k: v for k, v in os.environ.items() if v}}
    llm = dict(data.get("llm") or {})
    if env.get("OPENAI_BASE_URL"):
        llm["base_url"], llm["enabled"] = env["OPENAI_BASE_URL"], True
    if env.get("OPENAI_API_KEY"):
        llm["api_key"], llm["enabled"] = env["OPENAI_API_KEY"], True
    if env.get("SHEETIR_LLM_MODEL"):
        llm["model"] = env["SHEETIR_LLM_MODEL"]
    if env.get("SHEETIR_LLM_THINKING_PARAM"):
        llm["thinking_param"] = env["SHEETIR_LLM_THINKING_PARAM"]
    if env.get("SHEETIR_ALLOW_REMOTE_LLM"):
        llm["allow_remote"] = _truthy(env["SHEETIR_ALLOW_REMOTE_LLM"])
    paths = dict(data.get("paths") or {})
    if env.get("SHEETIR_LOG_DIR"):
        paths["log_dir"] = env["SHEETIR_LOG_DIR"]
    return Settings(**{**data, "llm": llm, "paths": paths, "source": str(path) if path else None})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The settings of this process, read once. Tests call get_settings.cache_clear() after changing them."""
    return load_settings()


# Host names that stay on this machine or the local network: Docker's host alias, mDNS names, single-label names
# (a LAN host or a Docker Compose service such as `ollama`).
_LOCAL_HOSTS = {"localhost", "host.docker.internal", "gateway.docker.internal"}


def is_local_url(url: str) -> bool:
    """Whether `url` points at this machine or the local network (loopback, private or link-local address, or a
    local host name). Anything else is a remote endpoint."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in _LOCAL_HOSTS or host.endswith((".local", ".localhost", ".internal", ".lan")) or "." not in host
    return ip.is_loopback or ip.is_private or ip.is_link_local


def llm_block_reason(s: Settings) -> str | None:
    """Why the configured model must not be used, or None when it may."""
    if not s.llm.enabled:
        return "no language model is configured (llm.enabled is false)"
    if not s.llm.allow_remote and not is_local_url(s.llm.base_url):
        return (f"{s.llm.base_url} is outside this machine and the local network; set llm.allow_remote "
                "(or SHEETIR_ALLOW_REMOTE_LLM=1) to send workbook contents there")
    return None


def apply_offline(s: Settings | None = None) -> None:
    """Keep libraries from reaching the network: no model downloads, no telemetry. Called at startup."""
    s = s or get_settings()
    if s.offline:
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                  "DO_NOT_TRACK", "NEXT_TELEMETRY_DISABLED"):
            os.environ.setdefault(k, "1")
