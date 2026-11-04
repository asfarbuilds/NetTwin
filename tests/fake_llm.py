"""A scripted stand-in for openai.OpenAI: returns queued replies and records the messages it was sent."""

from __future__ import annotations

import json
from types import SimpleNamespace


class FakeOpenAI:
    def __init__(self, replies: list[str], models: tuple[str, ...] = ("glm-5.3",)):
        self.replies = list(replies)
        self.sent: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=m) for m in models])

    def _create(self, model, messages, temperature):
        self.sent.append(messages)
        if not self.replies:
            raise AssertionError("FakeOpenAI ran out of scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def j(obj) -> str:
    return json.dumps(obj)
