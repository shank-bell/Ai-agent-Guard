"""Language-model backends for the LLM-driven agents.

`BedrockLLM` calls a real model through Amazon Bedrock (needs boto3 and AWS sign-in; both are optional, the rest of
the project does not need them). `ScriptedLLM` is a stand-in for tests and offline demos.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_MODEL_ID = "amazon.nova-micro-v1:0"
DEFAULT_REGION = "ap-southeast-2"


@dataclass
class LLMReply:
    text: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class LLM:
    model_id = "unknown"

    def complete(self, system: str, user: str) -> LLMReply:
        raise NotImplementedError


class BedrockLLM(LLM):
    """Amazon Bedrock Converse API. Temperature 0 so runs are as repeatable as a model allows."""

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, region: str = DEFAULT_REGION, client=None, max_tokens: int = 300):
        self.model_id, self.region, self.max_tokens = model_id, region, max_tokens
        if client is None:
            import boto3                       # optional dependency: only real-LLM mode needs it
            client = boto3.Session(region_name=region).client("bedrock-runtime")
        self.client = client

    def complete(self, system: str, user: str) -> LLMReply:
        started = time.monotonic()
        resp = self.client.converse(
            modelId=self.model_id, system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0})
        blocks = resp["output"]["message"]["content"]
        usage = resp.get("usage", {})
        return LLMReply("".join(b.get("text", "") for b in blocks), int((time.monotonic() - started) * 1000),
                        usage.get("inputTokens", 0), usage.get("outputTokens", 0))


class ScriptedLLM(LLM):
    """Answers with whatever `respond(system, user)` returns. No network."""

    def __init__(self, respond: Callable[[str, str], str], model_id: str = "scripted-model"):
        self.respond, self.model_id = respond, model_id

    def complete(self, system: str, user: str) -> LLMReply:
        return LLMReply(self.respond(system, user))


def parse_json_reply(text: str | None) -> dict | None:
    """Pull the first JSON object out of a model reply (models often wrap it in prose or code fences)."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
