"""Claude Code를 Remote Control 세션으로 띄우고 상태를 본다.

노리는 작업을 하지 않는다. 세션을 열어주기만 하고, 실제 작업은 사용자가
폰의 Claude 앱이나 claude.ai/code 에서 그 세션을 몰아서 한다.

서버 모드(`claude remote-control`)를 쓴다. 터미널 UI를 쓰는 게 아니라
원격 연결을 기다리는 프로세스라서 노리 용도에 맞고, 자격/정책 문제가 있으면
바로 종료하며 이유를 뱉어준다.
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

BOOT_WAIT = float(os.environ.get("NORI_BOOT_WAIT", "20"))
ANSI = re.compile(r"\x1b\[[0-9;>?]*[a-zA-Z]|\x1b[\]\[][^\x07]*\x07|[\x00-\x08\x0e-\x1f]")
SESSION_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]+")
TRUST_HINT = "I trust this folder"

# 문서상 이 변수들이 켜져 있으면 Remote Control 이 조용히 꺼진다.
BLOCKERS = ("DISABLE_TELEMETRY", "DO_NOT_TRACK",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK")


def _clean_env() -> dict[str, str]:
    """CLAUDE_CODE_CHILD_SESSION 이 상속되면 자식 세션의 transcript 저장이 꺼진다.
    그러면 노리가 작업 내용을 읽을 방법이 사라지므로 CLAUDE_CODE* 를 털어낸다.
    (CLAUDE_REMOTE_CONTROL_* 같은 사용자 설정은 남긴다.)"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE_CODE") and k != "CLAUDECODE"}


def preflight(cwd: str) -> list[str]:
    """띄우기 전에 Remote Control 을 막을 만한 조건을 찾아 문장으로 돌려준다."""
    bad = []
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base and "api.anthropic.com" not in base:
        bad.append(f"`ANTHROPIC_BASE_URL` 이 `{base}` 로 잡혀 있어 Remote Control 이 꺼진다.")
    for var in BLOCKERS:
        if os.environ.get(var):
            bad.append(f"`{var}` 가 켜져 있으면 Remote Control 이 꺼진다.")
    if os.path.realpath(cwd) == os.path.realpath(os.path.expanduser("~")):
        bad.append("홈 디렉터리는 신뢰가 저장되지 않는다. 프로젝트 폴더에서 열어야 한다.")
    return bad


@dataclass
class Live:
    cwd: str
    proc: subprocess.Popen
    master: int
    name: str
    url: str = ""
    chunks: deque = field(default_factory=lambda: deque(maxlen=400))

    def __post_init__(self):
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        """pty 버퍼가 차면 자식이 멈추므로 계속 비워준다."""
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
        return re.sub(r"[ \t]{2,}", " ", ANSI.sub(" ", raw))

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


def served() -> dict[str, Live]:
    return {k: v for k, v in _live.items() if v.alive}


# ------------------------------------------------------------------ 실행

@dataclass
class Launch:
    ok: bool
    message: str
    url: str = ""
    name: str = ""
    needs_trust: bool = False


def _error_from(screen: str) -> str:
    for line in screen.splitlines():
        line = line.strip()
        if line.lower().startswith("error") or "disabled" in line.lower():
            return line[:300]
    return (screen.strip()[-300:] or "이유를 알 수 없다.")


def launch(cwd: str, name: str | None = None) -> Launch:
    if warnings := preflight(cwd):
        return Launch(False, "띄우기 전에 걸리는 게 있어:\n- " + "\n- ".join(warnings))

    with _lock:
        if (held := _live.get(cwd)) and held.alive:
            return Launch(True, "이미 노리가 띄워둔 세션이 있어.", held.url, held.name)
        _live.pop(cwd, None)

        label = name or os.path.basename(cwd.rstrip("/")) or "project"
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["claude", "remote-control", "--name", label],
                cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, start_new_session=True, env=_clean_env(),
            )
        except (OSError, FileNotFoundError) as exc:
            os.close(master)
            os.close(slave)
            return Launch(False, f"세션을 띄우지 못했어: {exc}")
        os.close(slave)
        live = Live(cwd=cwd, proc=proc, master=master, name=label)
        _live[cwd] = live

    deadline = time.time() + BOOT_WAIT
    while time.time() < deadline:
        time.sleep(0.4)
        screen = live.screen()

        if TRUST_HINT in screen:
            return Launch(False,
                          f"`{cwd}` 는 Claude에서 처음 여는 폴더라 신뢰 확인을 묻고 있어.\n"
                          f"`!trust` 라고 답하면 승인할게.",
                          needs_trust=True, name=label)

        if (found := SESSION_URL.search(screen)):
            live.url = found.group(0)
            return Launch(True,
                          f"Remote Control 세션 열었어. 폰 Claude 앱 → Code 에서 "
                          f"`{label}` 로 잡히거나, 링크로 바로 들어가면 돼.",
                          live.url, label)

        if not live.alive:
            _live.pop(cwd, None)
            return Launch(False, f"Remote Control 을 켜지 못했어.\n> {_error_from(screen)}")

    return Launch(False,
                  f"{BOOT_WAIT:.0f}초 안에 세션 주소가 안 나왔어. 프로세스는 살아있으니 "
                  f"`!live` 로 다시 확인해줘.", name=label)


def approve_trust(cwd: str | None = None) -> str:
    """신뢰 확인 프롬프트에 '1. Yes' 를 눌러준다."""
    pending = [v for k, v in _live.items()
               if (cwd is None or k == cwd) and v.alive and TRUST_HINT in v.screen()]
    if not pending:
        return "지금 신뢰 확인을 기다리는 세션이 없어."
    for live in pending:
        live.send("1\r")
    time.sleep(3)
    lines = []
    for live in pending:
        if (found := SESSION_URL.search(live.screen())):
            live.url = found.group(0)
        lines.append(f"`{live.cwd}` → {live.url or '아직 주소 안 나옴'}")
    return "승인했어.\n" + "\n".join(lines)


def stop(cwd: str) -> str:
    live = _live.pop(cwd, None)
    if live is None:
        return "노리가 띄운 세션 중엔 그 폴더가 없어."
    live.stop()
    return f"`{cwd}` 세션 종료했어."
