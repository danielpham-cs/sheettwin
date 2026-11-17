import os

import pytest

from sheetir.config import Settings, apply_offline, is_local_url, llm_block_reason, load_settings


def test_defaults_without_a_file():
    s = Settings()
    assert s.llm.enabled is False and s.agent.max_steps == 8 and s.relations.family_jaccard == 0.9
    assert s.paths.log_dir == ".logs"


def test_yaml_is_read_and_unknown_keys_ignored(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("llm:\n  model: m1\n  max_concurrency: 3\nagent:\n  max_steps: 4\nfuture_section: {a: 1}\n",
                 encoding="utf-8")
    s = load_settings(p, env={})
    assert s.llm.model == "m1" and s.llm.max_concurrency == 3 and s.agent.max_steps == 4
    assert s.llm.base_url == "http://127.0.0.1:8000/v1" and s.source == str(p)


def test_missing_file_named_explicitly_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml", env={})


def test_bad_value_is_rejected(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("llm:\n  max_concurrency: 0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(p, env={})


def test_environment_overrides_and_turns_the_model_on(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("llm:\n  model: from-yaml\n", encoding="utf-8")
    s = load_settings(p, env={"OPENAI_BASE_URL": "http://192.168.1.20:11434/v1", "SHEETIR_LLM_MODEL": "qwen",
                              "SHEETIR_LOG_DIR": "/data/.logs", "SHEETIR_LLM_THINKING_PARAM": "think"})
    assert s.llm.enabled and s.llm.model == "qwen" and s.llm.base_url.startswith("http://192.168")
    assert s.llm.thinking_param == "think"
    assert s.paths.log_dir == "/data/.logs" and llm_block_reason(s) is None


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:8000/v1", True), ("http://localhost:11434/v1", True), ("http://[::1]:8000/v1", True),
    ("http://10.0.0.5/v1", True), ("http://192.168.0.9:8000", True), ("http://ollama:11434/v1", True),
    ("http://host.docker.internal:11434/v1", True), ("http://gpu-box.local:8000", True),
    ("https://api.openai.com/v1", False), ("http://8.8.8.8/v1", False), ("http://[2001:4860::8888]/v1", False),
    ("not a url", False),
])
def test_local_urls(url, local):
    assert is_local_url(url) is local


def test_remote_endpoint_needs_permission():
    s = Settings(llm={"enabled": True, "base_url": "https://api.openai.com/v1"})
    assert "allow_remote" in llm_block_reason(s)
    assert llm_block_reason(Settings(llm={"enabled": True, "base_url": "https://api.openai.com/v1",
                                          "allow_remote": True})) is None
    assert "no language model" in llm_block_reason(Settings())


def test_allow_remote_from_environment(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("{}", encoding="utf-8")
    s = load_settings(p, env={"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "https://api.openai.com/v1",
                              "SHEETIR_ALLOW_REMOTE_LLM": "yes"})
    assert llm_block_reason(s) is None


def test_offline_sets_library_switches(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    apply_offline(Settings(offline=True))
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_repo_settings_file_matches_the_defaults():
    from sheetir.config import REPO

    s = load_settings(REPO / "config" / "settings.yaml", env={})
    assert s.model_dump(exclude={"source"}) == Settings().model_dump(exclude={"source"})
