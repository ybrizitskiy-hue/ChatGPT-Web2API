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


async def _install_isolated_wrapper(monkeypatch, driver: _Driver):
    async def timed_out_original(self, text: str) -> None:
        assert text == driver.expected
        driver.insert_calls += 1
        raise TimeoutError("CDP timeout: Input.insertText")

    monkeypatch.setattr(ChatGPTDom, "type_message", timed_out_original)
    monkeypatch.setattr(input_recovery, "_INSTALLED", False)
    monkeypatch.setattr(input_recovery, "INSERT_TEXT_RECONCILE_TIMEOUT_SECONDS", 0.05)
    input_recovery.install_input_recovery()
    return ChatGPTDom(driver)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["match", "newline"])
async def test_insert_timeout_recovers_only_when_exact_text_is_visible(monkeypatch, mode):
    driver = _Driver("hello", mode)
    dom = await _install_isolated_wrapper(monkeypatch, driver)

    await dom.type_message("hello")

    assert driver.insert_calls == 1
    assert driver.probes >= 1


@pytest.mark.asyncio
async def test_insert_timeout_preserves_failure_when_composer_text_does_not_match(monkeypatch):
    driver = _Driver("hello", "mismatch")
    dom = await _install_isolated_wrapper(monkeypatch, driver)

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
