"""OpenAI API 얇은 래퍼 — 음성 인식(STT)과 채팅 호출."""

from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI

CHAT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe")
STT_FALLBACK = "whisper-1"

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()  # OPENAI_API_KEY 사용
    return _client


def transcribe(path: str | Path, language: str = "ko") -> str:
    """음성 파일을 텍스트로. 새 모델이 막혀 있으면 whisper-1로 되돌린다."""
    for model in (STT_MODEL, STT_FALLBACK):
        try:
            with open(path, "rb") as f:
                resp = client().audio.transcriptions.create(
                    model=model, file=f, language=language
                )
            return (resp.text or "").strip()
        except Exception:
            if model == STT_FALLBACK:
                raise
    return ""


def chat(messages: list[dict], tools: list[dict] | None = None):
    kwargs = {"model": CHAT_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return client().chat.completions.create(**kwargs).choices[0].message
