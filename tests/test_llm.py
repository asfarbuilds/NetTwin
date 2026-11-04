import pytest
from pydantic import SecretStr

from app.config import Settings
from app.llm import LLM, LLMError, extract_json, load_prompt, render
from app.schemas import RulesetProposal, TestPlan
from tests.fake_llm import FakeOpenAI, j

SETTINGS = Settings(vultr_model="glm-5.3", vultr_inference_api_key=SecretStr("test"), _env_file=None)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```\nHope that helps!',
        '<think>the user wants {"a": 2}... let me think</think>\n{"a": 1}',
        'Sure! Here is the plan:\n{"a": 1}\nLet me know.',
        '<THINK>unclosed reasoning {"a": 9}',  # unclosed think swallows everything -> no JSON
    ],
)
def test_extract_json(text):
    if "unclosed" in text:
        with pytest.raises(ValueError):
            extract_json(text)
    else:
        assert extract_json(text) == {"a": 1}


def test_extract_json_rejects_non_object():
    with pytest.raises(ValueError):
        extract_json("[1, 2]")


def test_render_uses_double_braces_and_leaves_nft_alone():
    out = render("req={{request}} rule: table inet t { chain c { iifname $IF_LAN } }", request="x")
    assert out == "req=x rule: table inet t { chain c { iifname $IF_LAN } }"
    with pytest.raises(KeyError):
        render("{{missing}}")


def test_all_prompts_load_and_render():
    for name, fields in [("plan", ["request", "topology", "baseline"]),
                         ("generate", ["request", "topology", "baseline", "plan", "feedback"]),
                         ("repair", ["error"]),
                         ("summarize", ["request", "verdict", "timeline", "conflict", "rationale", "evidence"])]:
        system, user = load_prompt(name)
        render(user, **{f: "x" for f in fields})  # every placeholder is filled, none left over
        if name != "repair":
            assert system


def test_generator_prompt_never_mentions_the_invariant():
    system, user = load_prompt("generate")
    text = (system + user).lower()
    assert "inv-1" not in text and "admin keeps ssh" not in text and "management access" not in text


def test_verify_model():
    LLM(SETTINGS, client=FakeOpenAI([], models=("glm-5.3", "x"))).verify_model()
    with pytest.raises(LLMError, match="not in /v1/models"):
        LLM(SETTINGS, client=FakeOpenAI([], models=("other",))).verify_model()


def test_verify_model_checks_the_key_because_models_endpoint_is_public():
    import httpx2
    import openai

    resp = httpx2.Response(401, request=httpx2.Request("POST", "https://x/v1/chat/completions"))
    bad_key = openai.AuthenticationError("Invalid API key", response=resp, body=None)
    with pytest.raises(LLMError, match="not the account API key"):
        LLM(SETTINGS, client=FakeOpenAI([], reject_key=bad_key)).verify_model()


@pytest.mark.parametrize("raw", ["abc123", " abc123 ", "abc123\r", '"abc123"', "'abc123'", ' "abc123"\r'])
def test_api_key_copy_paste_debris_is_stripped(raw):
    s = Settings(vultr_model="m", vultr_inference_api_key=raw, _env_file=None)
    assert s.vultr_inference_api_key.get_secret_value() == "abc123"


def test_missing_model_fails_loudly():
    with pytest.raises(LLMError):
        LLM(Settings(vultr_model="", _env_file=None), client=FakeOpenAI([]))


def test_json_call_one_repair_then_ok():
    fake = FakeOpenAI(["I think the plan is good", j({"probes": []})])
    plan, _ = LLM(SETTINGS, client=fake).json_call("plan", TestPlan, request="r", topology="t", baseline="b")
    assert plan.probes == []
    repair = fake.sent[1][-1]
    assert len(fake.sent) == 2 and repair["role"] == "user" and "could not be used" in repair["content"]


def test_json_call_schema_violation_triggers_repair_then_fails():
    bad = j({"probes": [{"id": "x", "source": "root", "kind": "tcp", "target": "nvr", "port": 1, "expect": "deny"}]})
    fake = FakeOpenAI([bad, bad])
    with pytest.raises(LLMError, match="after one repair"):
        LLM(SETTINGS, client=fake).json_call("plan", TestPlan, request="r", topology="t", baseline="b")
    assert len(fake.sent) == 2  # exactly one repair, no more


def test_ruleset_proposal_parses_escaped_newlines():
    fake = FakeOpenAI(['```json\n{"ruleset": "table inet filter {\\n}\\n", "rationale": "ok"}\n```'])
    prop, _ = LLM(SETTINGS, client=fake).json_call(
        "generate", RulesetProposal, request="r", topology="t", baseline="b", plan="[]", feedback="")
    assert prop.ruleset == "table inet filter {\n}\n"


def test_streamed_usage_and_reasoning_are_recorded():
    llm = LLM(SETTINGS, client=FakeOpenAI(['{"probes": []}']))
    llm.json_call("plan", TestPlan, request="r", topology="t", baseline="b")
    assert llm.usage.calls == 1 and llm.usage.prompt_tokens == 100 and llm.usage.completion_tokens == 50


def test_stall_is_retried_once_then_fails_with_hint(monkeypatch):
    import httpx2
    import openai

    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    stall = openai.APITimeoutError(request=httpx2.Request("POST", "https://x/v1/chat/completions"))
    fake = FakeOpenAI([stall, stall, j({"probes": []})])
    with pytest.raises(LLMError, match="try a faster model"):
        LLM(SETTINGS, client=fake).json_call("plan", TestPlan, request="r", topology="t", baseline="b")
    assert len(fake.sent) == 2  # one retry only, not three


def test_stall_mid_stream_from_httpx_is_handled(monkeypatch):
    import httpx2

    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    fake = FakeOpenAI([j({"probes": []})])

    def stalling(*a, **kw):
        fake.sent.append(kw.get("messages"))
        yield from ()
        raise httpx2.ReadTimeout("The read operation timed out")

    calls = iter([stalling(), None])
    orig = fake._create
    fake.chat.completions.create = lambda **kw: next(calls) or orig(**kw)
    LLM(SETTINGS, client=fake).json_call("plan", TestPlan, request="r", topology="t", baseline="b")


def test_hard_cap_per_call(monkeypatch):
    clock = iter([0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
    monkeypatch.setattr("app.llm.time.monotonic", lambda: next(clock))
    with pytest.raises(LLMError, match="exceeded"):
        LLM(SETTINGS, client=FakeOpenAI(['{"probes": []}'])).chat([{"role": "user", "content": "x"}], label="t")


def test_backoff_on_rate_limit(monkeypatch):
    import httpx2
    import openai

    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    req = httpx2.Request("POST", "https://x/v1/chat/completions")
    err = openai.RateLimitError("slow down", response=httpx2.Response(429, request=req), body=None)
    fake = FakeOpenAI([err, err, j({"probes": []})])
    LLM(SETTINGS, client=fake).json_call("plan", TestPlan, request="r", topology="t", baseline="b")
    assert len(fake.sent) == 3
