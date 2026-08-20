"""Claude Code를 Remote Control 세션으로 띄우고 상태를 본다.

헤드리스(`claude -p`)가 아니라 대화형 세션이라, 권한 승인은 폰의 Claude 앱에서 누르면 된다.
노리는 실행 주체가 아니라 '열어주는 사람'이다.
"""

from __future__ import annotations

import json
import os
import pty
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

BOOT_WAIT = float(os.environ.get("NORI_BOOT_WAIT", "10"))
ANSI = re.compile(r"\x1b\[[0-9;>?]*[a-zA-Z]|\x1b[\]\[][^\x07]*\x07|[\x00-\x08\x0e-\x1f]")
TRUST_HINT = "I trust this folder"


def _clean_env() -> dict[str, str]:
    """CLAUDE_CODE_CHILD_SESSION 이 상속되면 자식 세션의 transcript 저장이 꺼진다.
    그러면 노리가 작업 내용을 읽을 방법이 사라지므로 CLAUDE* 변수를 전부 털어낸다."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


@dataclass
class Live:
    cwd: str
    proc: subprocess.Popen
    master: int
    name: str
    chunks: deque = field(default_factory=lambda: deque(maxlen=400))

    def __post_init__(self):
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        """pty 버퍼가 차면 TUI가 멈추므로 계속 비워준다."""
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.chunks.append(data)

    def screen(self) -> str:
        raw = b"".join(self.chunks).decode("utf-8", "replace")
        return re.sub(r"\s{2,}", " ", ANSI.sub(" ", raw))

    def send(self, keys: str) -> None:
        os.write(self.master, keys.encode())

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        pid = self.proc.pid
        try:
            # start_new_session=True 면 자식이 그룹 리더다. 그 사실을 확인했을 때만
            # 그룹째 종료한다 — 아니면 남의 세션까지 같이 죽을 수 있다.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                self.proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass


_live: dict[str, Live] = {}     # cwd -> Live
_lock = threading.Lock()


# ------------------------------------------------------------------ 조회

def agents() -> list[dict]:
    """실행 중인 Claude 세션 목록. TTY 없이도 된다."""
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"], capture_output=True, text=True,
            timeout=20, env=_clean_env(),
        ).stdout
        return json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def agent_for(cwd: str) -> dict | None:
    for a in agents():
        if a.get("cwd") == cwd:
            return a
    return None


# ------------------------------------------------------------------ 실행

@dataclass
class Launch:
    ok: bool
    message: str
    session_id: str = ""
    name: str = ""
    needs_trust: bool = False


def launch(cwd: str, name: str | None = None) -> Launch:
    with _lock:
        held = _live.get(cwd)
        if held and held.alive:
            info = agent_for(cwd) or {}
            return Launch(True, "이미 열려 있는 세션이야.",
                          info.get("sessionId", ""), held.name)
        if held:
            _live.pop(cwd, None)

        if (info := agent_for(cwd)):
            return Launch(True, "이미 Claude가 켜져 있는 폴더야.",
                          info.get("sessionId", ""), info.get("name", ""))

        label = name or ""
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["claude", "--remote-control"] + ([label] if label else []),
                cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, start_new_session=True, env=_clean_env(),
            )
        except (OSError, FileNotFoundError) as exc:
            os.close(master)
            os.close(slave)
            return Launch(False, f"세션을 띄우지 못했어: {exc}")
        os.close(slave)

        live = Live(cwd=cwd, proc=proc, master=master,
                    name=label or os.path.basename(cwd))
        _live[cwd] = live

    deadline = time.time() + BOOT_WAIT
    while time.time() < deadline:
        time.sleep(0.4)
        if not live.alive:
            return Launch(False, f"세션이 바로 죽었어.\n```\n{live.screen()[-500:]}\n```")
        if TRUST_HINT in live.screen():
            return Launch(
                False,
                f"`{cwd}` 는 Claude에서 처음 여는 폴더라 신뢰 확인을 묻고 있어.\n"
                f"`!trust` 라고 답하면 승인할게. (아니면 터미널에서 한 번 열어주면 돼)",
                needs_trust=True, name=label,
            )
        if (info := agent_for(cwd)):
            return Launch(True, "Remote Control 세션 열었어. Claude 앱에서 잡힐 거야.",
                          info.get("sessionId", ""), info.get("name", label))

    return Launch(True, "세션은 떴는데 아직 목록에 안 잡혀. 잠깐 뒤에 다시 봐줘.", name=label)


def approve_trust(cwd: str | None = None) -> str:
    """신뢰 확인 프롬프트에 '1. Yes' 를 눌러준다."""
    targets = [v for k, v in _live.items() if (cwd is None or k == cwd) and v.alive]
    pending = [t for t in targets if TRUST_HINT in t.screen()]
    if not pending:
        return "지금 신뢰 확인을 기다리는 세션이 없어."
    for live in pending:
        live.send("1\r")
    time.sleep(2)
    return "승인했어. " + ", ".join(f"`{t.cwd}`" for t in pending)


def stop(cwd: str) -> str:
    live = _live.pop(cwd, None)
    if live is None:
        return "노리가 띄운 세션 중엔 그 폴더가 없어."
    live.stop()
    return f"`{cwd}` 세션 종료했어."


def held() -> list[Live]:
    return [v for v in _live.values() if v.alive]
