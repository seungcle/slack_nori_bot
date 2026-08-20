"""Nori — Slack에서 말하면(음성 포함) GPT가 먼저 듣는 비서.

노리는 코드를 고치지 않는다. 프로젝트를 Claude Remote Control 세션으로 열어주고,
거기서 벌어진 일을 읽어와 같이 이야기한다.
"""

import os
import re
import tempfile
import threading

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

import brain          # noqa: E402  — .env 를 읽은 뒤에 불러야 한다
import claude_runner  # noqa: E402
import gpt            # noqa: E402

app = App(token=os.environ["SLACK_BOT_TOKEN"])

AUDIO_EXTS = {"m4a", "mp3", "mp4", "mpga", "wav", "webm", "ogg", "oga", "amr", "flac"}

# Slack 한도는 글자 수가 아니라 UTF-8 바이트 기준이다. 한글은 글자당 3바이트라
# 글자 수로 재면 msg_too_long 이 난다.
CHUNK_BYTES = 2800
MAX_CHUNKS = 8


def _clip(text: str, limit: int = CHUNK_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode("utf-8", "ignore") + "…"


def _chunks(text: str, limit: int = CHUNK_BYTES) -> list[str]:
    """UTF-8 바이트 기준으로 자르되 되도록 줄 단위로 끊는다."""
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:          # 한 줄이 통째로 클 때
            head = line.encode("utf-8")[:limit].decode("utf-8", "ignore")
            if buf:
                out.append(buf)
                buf = ""
            out.append(head)
            line = line[len(head):]
        if len((buf + line).encode("utf-8")) > limit:
            out.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out or [""]


# --------------------------------------------------------------- Slack 출력

def _sink(channel: str, client) -> brain.Sink:
    def say(text: str) -> str:
        parts = _chunks(text)[:MAX_CHUNKS]
        ts = ""
        for i, part in enumerate(parts):
            head = f"_({i + 1}/{len(parts)})_\n" if len(parts) > 1 else ""
            sent = client.chat_postMessage(channel=channel, text=head + part)
            ts = ts or sent["ts"]
        return ts

    return brain.Sink(say=say)


# ------------------------------------------------------------------ 명령어

ALIASES = {
    "clear": "clear", "초기화": "clear", "reset": "clear",
    "help": "help", "명령어": "help", "commands": "help", "?": "help",
    "projects": "projects", "sessions": "projects", "세션": "projects",
    "live": "live", "실행": "live", "running": "live",
    "trust": "trust", "신뢰": "trust", "승인": "trust",
}


def _command_of(text: str) -> str | None:
    """`/clear` `!clear` `.clear` 를 모두 같은 명령으로 본다."""
    if not text or text[0] not in "/!.":
        return None
    word = text[1:].split()
    return ALIASES.get(word[0].lower()) if word else None


def _run_command(name: str, channel: str, say) -> None:
    if name == "clear":
        brain.clear(channel)
        say("🧹 이 채널 대화 기억을 지웠어. 처음부터 다시.")
    elif name == "help":
        lines = "\n".join(f"`{k}` — {v}" for k, v in brain.COMMANDS.items())
        say(
            "*명령어*\n" + lines +
            "\n\n_`!` `.` `/` 아무 걸로 시작해도 되고 한국어(`!초기화`, `!명령어`, `!실행`)도 먹혀._"
            "\n그 밖엔 그냥 말하거나 음성 보내면 내가 알아서 판단할게."
        )
    elif name == "projects":
        say("*Claude 프로젝트 / 최근 세션*\n```\n" + brain.catalog() + "\n```")
    elif name == "live":
        say("*지금 켜져 있는 Claude 세션*\n```\n" + brain.live_list() + "\n```")
    elif name == "trust":
        say(claude_runner.approve_trust())


# ------------------------------------------------------------------ 음성 처리

def _audio_files(event: dict) -> list[dict]:
    out = []
    for f in event.get("files") or []:
        if (f.get("mimetype", "").startswith("audio/")
                or f.get("filetype") in AUDIO_EXTS
                or f.get("subtype") == "slack_audio"):
            out.append(f)
    return out


def _transcribe(f: dict) -> str:
    url = f.get("url_private_download") or f["url_private"]
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {app.client.token}"}, timeout=120
    )
    resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix="." + (f.get("filetype") or "m4a"),
                                     delete=False) as tmp:
        tmp.write(resp.content)
        path = tmp.name
    try:
        return gpt.transcribe(path)
    finally:
        os.unlink(path)


# -------------------------------------------------------------------- 이벤트

@app.event("message")
def handle_message(event, client, logger):
    if event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted"}:
        return

    channel = event["channel"]
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    audio = _audio_files(event)
    sink = _sink(channel, client)

    if not audio and not text:
        return

    def work():
        try:
            spoken = text
            if audio:
                heard = client.chat_postMessage(channel=channel, text="🎧 듣는 중…")
                try:
                    spoken = " ".join(
                        x for x in (_transcribe(audio[0]), text) if x
                    ).strip()
                except Exception as exc:
                    logger.exception("STT 실패")
                    client.chat_update(channel=channel, ts=heard["ts"],
                                       text=f"음성 인식에 실패했어: {exc}")
                    return
                client.chat_update(channel=channel, ts=heard["ts"],
                                   text=_clip(f"🎙️ “{spoken}”"))

            command = _command_of(spoken)
            if command:
                _run_command(command, channel, sink.say)
                return

            brain.bind(channel, sink)
            brain.handle(channel, spoken)
        except Exception as exc:
            logger.exception("처리 실패")
            sink.say(f"❌ 처리 중에 터졌어: {exc}")

    threading.Thread(target=work, daemon=True).start()


# Slack 앱에 슬래시 명령을 등록해 뒀다면 이쪽으로도 들어온다.
def _slash(name: str):
    def handler(ack, body, client):
        ack()
        channel = body["channel_id"]
        _run_command(name, channel, _sink(channel, client).say)
    return handler


for _cmd, _name in (("/clear", "clear"), ("/help", "help"), ("/projects", "projects"),
                    ("/live", "live"), ("/trust", "trust")):
    app.command(_cmd)(_slash(_name))


if __name__ == "__main__":
    print("Nori is listening...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
