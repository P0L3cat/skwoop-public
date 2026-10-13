"""deep_model: the per-turn model/reasoning line becomes Deep api_server model_options, or nothing at all."""
from __future__ import annotations

import pytest

import harness.deep_model as deep_model
from harness.deep_model import TurnModel, parse_turn_model, request_body


@pytest.mark.parametrize(("line", "want"), [
    ("low", TurnModel(None, "low")),
    ("grok-4.7 low", TurnModel("grok-4.7", "low")),
    ("grok-4.6 medium", TurnModel("grok-4.6", "medium")),
    ("", None), ("fast", None), ("grok-4.7", None), ("grok 4.7 low", None), ("Grok-4.7 low", None),
])
def test_parse_turn_model(line, want):
    assert parse_turn_model(line) == want


def test_the_file_is_read_per_turn_and_no_file_means_the_config_default(tmp_path):
    f = tmp_path / "deep-reasoning"
    assert deep_model.turn_model(f) is None
    f.write_text("grok-4.7 low\n")
    assert deep_model.turn_model(f) == TurnModel("grok-4.7", "low")
    f.write_text("loud")
    assert deep_model.turn_model(f) is None
    assert set(request_body("hi", None)) == {"model", "stream", "messages"}


def test_effort_only_keeps_the_deep_model():
    body = request_body("hi", TurnModel(None, "low"))
    assert body["model"] == "deep" and "provider" not in body
    assert body["model_options"] == {"reasoning": {"enabled": True, "effort": "low"}}


def test_a_model_comes_with_its_provider(monkeypatch):
    monkeypatch.delenv("DEEP_MODEL_PROVIDER", raising=False)
    body = request_body("hi", TurnModel("grok-4.7", "low"))
    assert body["model"] == "grok-4.7" and "provider" not in body
    monkeypatch.setenv("DEEP_MODEL_PROVIDER", "openai")
    body = request_body("hi", TurnModel("grok-4.7", "low"))
    assert body["provider"] == "openai"
