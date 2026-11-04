"""A scripted stand-in for openai.OpenAI: returns queued replies and records the messages it was sent."""

from __future__ import annotations

import json
from types import SimpleNamespace


class FakeOpenAI:
    def __init__(self, replies: list[str], models: tuple[str, ...] = ("glm-5.3",), reject_key: Exception | None = None):
        self.replies = list(replies)
        self.reject_key = reject_key
        self.sent: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=m) for m in models])

    def _create(self, model, messages, temperature=None, max_tokens=None, stream=False):
        if max_tokens == 1:  # verify_model's auth ping: not part of the scripted conversation
            if self.reject_key:
                raise self.reject_key
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="p"))], usage=None)
        assert stream, "chat() must stream"
        self.sent.append(messages)
        if not self.replies:
            raise AssertionError("FakeOpenAI ran out of scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _chunks(reply)


def _delta(content=None, reasoning=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content,
                                                                          reasoning_content=reasoning))], usage=None)


def _chunks(reply: str):
    """Stream like a reasoning model: thinking first, then the answer in two pieces, then a usage-only chunk."""
    half = len(reply) // 2
    yield _delta(reasoning="let me think about the topology...")
    yield _delta(content=reply[:half])
    yield _delta(content=reply[half:])
    yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))


def j(obj) -> str:
    return json.dumps(obj)
