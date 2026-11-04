"""Vultr Serverless Inference client (OpenAI-compatible). Every agent LLM call goes through here.

Rules (CLAUDE.md §6): never hardcode model IDs (VULTR_MODEL is verified against /v1/models at startup);
don't rely on response_format/JSON mode; strip <think> blocks and code fences, then parse with Pydantic;
one repair retry, then fail cleanly. Temperature 0.2, 60 s per call, backoff on 429/5xx.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
TEMPERATURE = 0.2
TIMEOUT_S = 60
BACKOFF_S = (2, 5, 10)  # waits between retries of 429 / 5xx / connection errors
USER_MARKER = "---USER---"

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- prompts

def load_prompt(name: str) -> tuple[str, str]:
    """A prompt file is '<system text>\\n---USER---\\n<user template>'. Placeholders are {{name}}
    (not str.format/$var, because nft rulesets are full of braces and $variables)."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    system, _, user = text.partition(USER_MARKER)
    return system.strip(), user.strip()


def render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if leftover := re.findall(r"\{\{(\w+)\}\}", template):
        raise KeyError(f"unfilled prompt placeholders: {leftover}")
    return template


# ---------------------------------------------------------------- JSON extraction

_THINK = re.compile(r"<think>.*?(</think>|\Z)", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def extract_json(text: str) -> dict:
    """Find the JSON object in a model reply: drop <think> blocks, prefer a fenced block,
    then decode from the first '{'. Raises ValueError."""
    text = strip_reasoning(text)
    if m := _FENCE.search(text):
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    obj, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("reply JSON is not an object")
    return obj


# ---------------------------------------------------------------- client

@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    per_call: list[dict] = field(default_factory=list)


class LLM:
    def __init__(self, settings: Settings, client: openai.OpenAI | None = None):
        self.model = settings.vultr_model
        if not self.model:
            raise LLMError("VULTR_MODEL is not set")
        self.client = client or openai.OpenAI(
            base_url=settings.vultr_inference_base_url,
            api_key=settings.vultr_inference_api_key.get_secret_value() or None,
            timeout=TIMEOUT_S,
            max_retries=0,  # we do our own backoff, so it's visible in logs
        )
        self.usage = Usage()

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list())

    def verify_model(self) -> None:
        """Fail loudly if VULTR_MODEL isn't served or the key is rejected. Never guess or invent model IDs.

        /v1/models answers without authentication, so the key is checked separately with a 1-token completion."""
        ids = self.list_models()
        if self.model not in ids:
            raise LLMError(f"VULTR_MODEL={self.model!r} is not in /v1/models. Available: {', '.join(ids)}")
        try:
            self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
            )
        except openai.AuthenticationError as e:
            raise LLMError(
                "Vultr Inference rejected VULTR_INFERENCE_API_KEY (401). Use the key of the Serverless Inference "
                "subscription (Products → Serverless → Inference), not the account API key."
            ) from e
        log.info("LLM: %s on Vultr Serverless Inference (key accepted)", self.model)

    def chat(self, messages: list[dict], label: str = "") -> str:
        for attempt in range(len(BACKOFF_S) + 1):
            t0 = time.monotonic()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=TEMPERATURE
                )
                break
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError) as e:
                if attempt == len(BACKOFF_S):
                    raise LLMError(f"Vultr Inference unavailable after retries: {type(e).__name__}") from e
                wait = BACKOFF_S[attempt]
                log.warning("LLM %s: %s, retrying in %ss", label, type(e).__name__, wait)
                time.sleep(wait)
            except openai.APIStatusError as e:  # 4xx other than 429: not retryable
                raise LLMError(f"Vultr Inference error {e.status_code}: {str(e)[:200]}") from e

        dt = time.monotonic() - t0
        content = resp.choices[0].message.content or ""
        u = getattr(resp, "usage", None)
        pt, ct = (getattr(u, "prompt_tokens", 0) or 0), (getattr(u, "completion_tokens", 0) or 0)
        self.usage.calls += 1
        self.usage.prompt_tokens += pt
        self.usage.completion_tokens += ct
        self.usage.seconds += dt
        self.usage.per_call.append({"label": label, "prompt_tokens": pt, "completion_tokens": ct, "s": round(dt, 1)})
        log.info("LLM %s: %.1fs, %d in / %d out tokens", label, dt, pt, ct)
        return content

    def json_call(self, prompt: str, schema: type[M], label: str = "", **values: str) -> tuple[M, str]:
        """Ask for JSON and parse it into a closed schema. One repair retry, then LLMError."""
        system, user_tpl = load_prompt(prompt)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": render(user_tpl, **values)}]
        raw = self.chat(messages, label=label)
        try:
            return schema.model_validate(extract_json(raw)), raw
        except (ValueError, ValidationError) as first_error:
            log.warning("LLM %s: unparseable reply, sending one repair request: %s", label, str(first_error)[:200])
            r_system, r_user = load_prompt("repair")
            messages += [
                {"role": "assistant", "content": strip_reasoning(raw)[:8000]},
                {"role": "user", "content": render(r_user, error=str(first_error)[:1500])},
            ]
            raw2 = self.chat(messages, label=f"{label}:repair")
            try:
                return schema.model_validate(extract_json(raw2)), raw2
            except (ValueError, ValidationError) as e:
                raise LLMError(f"{label}: model reply still invalid after one repair: {str(e)[:300]}") from e

    def text_call(self, prompt: str, label: str = "", **values: str) -> str:
        system, user_tpl = load_prompt(prompt)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": render(user_tpl, **values)}]
        return strip_reasoning(self.chat(messages, label=label))
