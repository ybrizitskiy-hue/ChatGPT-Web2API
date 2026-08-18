import json

import pytest

from chatgpt_web2api import input_recovery
from chatgpt_web2api.chatgpt_dom import ChatGPTDom


class _Driver:
    def __init__(self, expected: str, mode: str) -> None:
        self.expected = expected
        self.mode = mode
        self.insert_calls = 0
        self.probes = 0

    async def _cdp(self, method, params):
        assert method == "Input.insertText"
        assert params == {"text": self.expected}
        self.insert_calls += 1
        raise TimeoutError("CDP timeout: Input.insertText")

    async def _js_strict(self, expr, timeout=5):
        assert "querySelectorAll" in expr
        assert timeout == 5
        self.probes += 1
        if self.mode == "match":
            return json.dumps({"ready": True, "text": self.expected})
        if self.mode == "newline":
            return json.dumps({"ready": True, "text": self.expected + "\n"})
        if self.mode == "mismatch":
            return json.dumps({"ready": True, "text": "old text"})
        return json.dumps({"ready": False, "text": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["match", "newline"])
async def test_insert_timeout_recovers_only_when_exact_text_is_visible(monkeypatch, mode):
    monkeypatch.setattr(input_recovery, "INSERT_TEXT_RECONCILE_TIMEOUT_SECONDS", 0.05)
    driver = _Driver("hello", mode)
    dom = ChatGPTDom(driver)

    original = input_recovery._INSTALLED
    try:
        # The package installs the production wrapper at import time already.
        await dom.type_message("hello")
    finally:
        input_recovery._INSTALLED = original

    assert driver.insert_calls == 1
    assert driver.probes >= 1


@pytest.mark.asyncio
async def test_insert_timeout_preserves_failure_when_composer_text_does_not_match(monkeypatch):
    monkeypatch.setattr(input_recovery, "INSERT_TEXT_RECONCILE_TIMEOUT_SECONDS", 0.05)
    driver = _Driver("hello", "mismatch")
    dom = ChatGPTDom(driver)

    with pytest.raises(TimeoutError, match="Input.insertText"):
        await dom.type_message("hello")

    assert driver.insert_calls == 1
    assert driver.probes >= 1


def test_input_timeout_classifier_is_narrow():
    assert input_recovery._is_input_insert_timeout(
        TimeoutError("CDP timeout: Input.insertText")
    )
    assert not input_recovery._is_input_insert_timeout(
        TimeoutError("CDP timeout: Page.navigate")
    )
    assert not input_recovery._is_input_insert_timeout(RuntimeError("Input.insertText"))


def test_canonical_text_matches_editor_normalization():
    assert input_recovery._canonical_text("a\r\nb\u00a0c") == "a\nb c"
