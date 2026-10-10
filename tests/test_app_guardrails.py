from __future__ import annotations

from pathlib import Path

from app.guardrails import MAX_NEW_TOKENS, Limits, inspect
from gateway.config import load_config
from gateway.serve import listed_models, prepare_chat

SERVED = Limits(models=frozenset({"qwen-27b", "qwen-9b"}))


def test_unknown_model_refuses() -> None:
    g = inspect({"model": "some-cloud-model", "messages": [{"role": "user", "content": "hi"}]}, SERVED)
    assert not g.ok and g.status == 400 and g.reason == "model_not_allowed"


def test_served_model_passes_and_max_tokens_is_capped() -> None:
    g = inspect({"model": "qwen-9b", "messages": [{"role": "user", "content": "GPU?"}], "max_tokens": 9_999}, SERVED)
    assert g.ok
    assert g.payload["max_tokens"] == MAX_NEW_TOKENS
    assert g.payload["tenant"] == "lab"


def test_empty_prompt_and_bad_max_tokens() -> None:
    empty = inspect({"model": "qwen-9b", "messages": [{"role": "user", "content": "  "}]}, SERVED)
    assert not empty.ok and empty.reason == "empty_prompt"
    bad = inspect({"model": "qwen-9b", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 0}, SERVED)
    assert not bad.ok and bad.reason == "bad_max_tokens"


def test_image_part_needs_no_text() -> None:
    g = inspect(
        {
            "model": "qwen-9b",
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]}],
        },
        SERVED,
    )
    assert g.ok


def test_models_lists_each_pool_with_its_context() -> None:
    cfg = load_config(Path(__file__).resolve().parents[1] / "gateway" / "config-27b-9b.yaml")
    rows = listed_models(cfg)["data"]
    assert {r["id"] for r in rows} == set(cfg.models())
    assert all(r["max_model_len"] > 0 for r in rows)


def test_orch_serve_applies_edge_before_admit() -> None:
    status, body = prepare_chat({"model": "some-cloud-model", "messages": [{"role": "user", "content": "hi"}]}, SERVED)
    assert status == 400 and body["error"]["type"] == "model_not_allowed"
    status, body = prepare_chat({"model": "qwen-9b", "messages": [{"role": "user", "content": "GPU?"}]}, SERVED)
    assert status == 200 and body["model"] == "qwen-9b"
