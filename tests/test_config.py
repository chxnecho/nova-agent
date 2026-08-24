"""Tests for nova.config."""

from nova.config import Config


def make_cfg(data):
    return Config(data)


def test_get_with_dots():
    cfg = make_cfg({"llm": {"model": "m1", "temperature": 0.5}})
    assert cfg.get("llm.model") == "m1"
    assert cfg.get("llm.temperature") == 0.5
    assert cfg.get("llm.missing", "fallback") == "fallback"


def test_attr_access_nested():
    cfg = make_cfg({"a": {"b": {"c": 1}}})
    assert cfg.a.b.c == 1


def test_repr_hides_keys():
    cfg = make_cfg({"api_key": "secret", "model": "x"})
    r = repr(cfg)
    assert "secret" not in r
    assert "***" in r
