"""Tiny OpenAI-compatible client (stdlib only) for the local model.

Inside the sandbox the base URL is https://inference.local/v1 (OpenShell route -> Ollama on the GB10); urllib picks
up HTTPS_PROXY and the OpenShell CA from the sandbox environment. On the host, set
OUTREACH_LLM_BASE_URL=http://localhost:11434/v1 and OUTREACH_LLM_MODEL=nemotron-3.5-lightning:30b.
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import config

_CFG = None


def _cfg():
    global _CFG
    if _CFG is None:
        _CFG = config.load()
    return _CFG


def _trace(run, name, messages, output, secs, status="ok"):
    if run is not None:
        last = messages[-1]["content"] if messages else ""
        run.step("llm", name, {"system": (messages[0]["content"][:300] if messages and messages[0]["role"] == "system" else None),
                               "prompt": last if isinstance(last, str) else str(last)},
                 output, int(secs * 1000), status)


def chat(messages, max_tokens=900, temperature=0.4, json_mode=False, retries=2, run=None, name="llm"):
    """Returns (text, model, seconds). Pass run=traces.Run to record the call as a trace step."""
    cfg = _cfg()
    payload = {"model": cfg["llm_model"], "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "reasoning_effort": "none", "stream": False}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(cfg["llm_base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=cfg["llm_timeout"]) as r:
                body = json.load(r)
            msg = body["choices"][0]["message"]
            text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
            if text:
                secs = round(time.time() - t0, 2)
                _trace(run, name, messages, text, secs)
                return text, body.get("model", cfg["llm_model"]), secs
            last = RuntimeError("empty completion")
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            last = e
        time.sleep(1 + attempt)
    _trace(run, name, messages, repr(last), 0, "error")
    raise RuntimeError(f"LLM call failed: {last}")


def chat_tools(messages, tools, run=None, name="orchestrator", max_tokens=1200, temperature=0.2):
    """One tool-calling turn. Returns (assistant_message_dict, model, seconds)."""
    cfg = _cfg()
    payload = {"model": cfg["llm_model"], "messages": messages, "tools": tools, "max_tokens": max_tokens,
               "temperature": temperature, "reasoning_effort": "none", "stream": False}
    req = urllib.request.Request(cfg["llm_base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=cfg["llm_timeout"]) as r:
        body = json.load(r)
    msg = body["choices"][0]["message"]
    out = {"role": "assistant", "content": re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()}
    if msg.get("tool_calls"):
        out["tool_calls"] = [{"id": t.get("id") or f"call_{i}", "type": "function",
                              "function": {"name": t["function"]["name"], "arguments": t["function"].get("arguments") or "{}"}}
                             for i, t in enumerate(msg["tool_calls"])]
    secs = round(time.time() - t0, 2)
    if run is not None:
        run.step("llm", name, {"prompt": messages[-1].get("content") if messages else None,
                               "tools_offered": [t["function"]["name"] for t in tools]},
                 {"content": out["content"], "tool_calls": [{"name": t["function"]["name"], "args": t["function"]["arguments"]}
                                                            for t in out.get("tool_calls", [])]}, int(secs * 1000))
    return out, body.get("model", cfg["llm_model"]), secs


def chat_json(messages, required=(), **kw):  # kw may include run=, name=
    """Chat expecting a JSON object; tolerant of code fences / prose around it."""
    text, model, secs = chat(messages, json_mode=True, **kw)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    data = json.loads(m.group(0))
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(f"model JSON missing {missing}")
    return data, model, secs
