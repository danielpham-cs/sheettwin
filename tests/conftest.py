import pytest

import sheetir.web.app


@pytest.fixture(autouse=True)
def no_language_model(monkeypatch):
    """Tests never reach a real model, whatever .env or OPENAI_API_KEY says; a test that wants one patches it in."""
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: None)
