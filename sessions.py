"""~/.claude/projects 아래에 쌓인 Claude Code 세션을 훑어보는 헬퍼."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
HEAD_LINES = 80          # cwd / 첫 사용자 메시지를 찾으려고 읽는 최대 줄 수
CACHE_TTL = 30.0         # 초


@dataclass(frozen=True)
class Session:
    sid: str
    cwd: str
    title: str
    mtime: float

    @property
    def when(self) -> str:
        return time.strftime("%m/%d %H:%M", time.localtime(self.mtime))

    def label(self, width: int = 72) -> str:
        title = self.title or "(제목 없음)"
        return _clip(f"{self.when} · {title}", width)


def _clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _peek(jsonl: Path) -> tuple[str, str]:
    """세션 파일 앞부분에서 (cwd, 첫 사용자 메시지)를 뽑아낸다."""
    cwd = title = ""
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(HEAD_LINES), f):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = cwd or rec.get("cwd") or ""
                if not title and rec.get("type") == "user" and not rec.get("isSidechain"):
                    title = _message_text(rec.get("message") or {}).strip()
                if cwd and title:
                    break
    except OSError:
        pass
    return cwd, title


def _fallback_cwd(dir_name: str) -> str:
    """cwd 기록이 없을 때 디렉터리 이름에서 경로를 복원(하이픈이 섞이면 부정확)."""
    return "/" + dir_name.lstrip("-").replace("-", "/")


_cache: dict[str, object] = {"at": 0.0, "sessions": []}


def all_sessions(force: bool = False) -> list[Session]:
    now = time.time()
    if not force and now - float(_cache["at"]) < CACHE_TTL:
        return list(_cache["sessions"])  # type: ignore[arg-type]

    found: list[Session] = []
    if PROJECTS_DIR.is_dir():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.glob("*.jsonl"):
                cwd, title = _peek(jsonl)
                found.append(
                    Session(
                        sid=jsonl.stem,
                        cwd=cwd or _fallback_cwd(project_dir.name),
                        title=title,
                        mtime=jsonl.stat().st_mtime,
                    )
                )
    found.sort(key=lambda s: s.mtime, reverse=True)
    _cache["at"], _cache["sessions"] = now, found
    return list(found)


def pid_of(cwd: str) -> str:
    """Slack 옵션 value 길이 제한(75자)을 피하려고 경로를 짧은 id로 접는다."""
    return hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:12]


def projects(limit: int = 20) -> list[tuple[str, int, float]]:
    """최근에 손댄 순서로 (cwd, 세션 수, 마지막 수정 시각)."""
    seen: dict[str, list] = {}
    for s in all_sessions():
        row = seen.setdefault(s.cwd, [0, s.mtime])
        row[0] += 1
        row[1] = max(row[1], s.mtime)
    ordered = sorted(seen.items(), key=lambda kv: kv[1][1], reverse=True)
    return [(cwd, n, mtime) for cwd, (n, mtime) in ordered[:limit]]


def cwd_for_pid(pid: str) -> str | None:
    for cwd, _, _ in projects(limit=10_000):
        if pid_of(cwd) == pid:
            return cwd
    return None


def sessions_for(cwd: str, limit: int = 20) -> list[Session]:
    return [s for s in all_sessions() if s.cwd == cwd][:limit]


# ------------------------------------------------- 세션에서 실제로 무슨 일이 있었나

TAIL_BYTES = 2_000_000     # 큰 세션 파일은 끝부분만 읽는다


def find_jsonl(sid: str) -> Path | None:
    """세션 id(앞자리만 줘도 됨)로 transcript 파일을 찾는다."""
    if not PROJECTS_DIR.is_dir():
        return None
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.stem.startswith(sid):
                return jsonl
    return None


def _tail_records(jsonl: Path) -> list[dict]:
    with jsonl.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        blob = f.read()
    lines = blob.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]                    # 잘린 첫 줄은 버린다
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _blocks(message: dict) -> tuple[str, list[str]]:
    """(본문 텍스트, 쓴 도구 이름들)"""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    text, tools = [], []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tools.append(b.get("name", "?"))
    return " ".join(text).strip(), tools


def recent_turns(sid: str, limit: int = 12, budget: int = 6000) -> str:
    """세션에서 최근 대화를 사람이 읽을 수 있는 형태로 뽑는다."""
    jsonl = find_jsonl(sid)
    if jsonl is None:
        return ""

    turns = []
    for rec in _tail_records(jsonl):
        kind = rec.get("type")
        if kind not in {"user", "assistant"} or rec.get("isSidechain"):
            continue
        text, tools = _blocks(rec.get("message") or {})
        if not text and not tools:
            continue
        who = "👤" if kind == "user" else "🤖"
        mark = f" [도구: {', '.join(dict.fromkeys(tools))}]" if tools else ""
        turns.append(f"{who} {_clip(text, 700)}{mark}")

    turns = turns[-limit:]
    while turns and len("\n".join(turns)) > budget:
        turns.pop(0)
    return "\n".join(turns)
