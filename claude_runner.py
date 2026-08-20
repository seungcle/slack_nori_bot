"""Claude Code CLI를 헤드리스로 돌려서 특정 세션에 이어붙인다."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

# acceptEdits: 파일 편집은 자동 승인, Bash 등은 여전히 막힌다.
# bypassPermissions: 전부 통과 — 신뢰하는 폴더에서만.
PERMISSION_MODE = os.environ.get("NORI_PERMISSION_MODE", "acceptEdits")
TIMEOUT = int(os.environ.get("NORI_CLAUDE_TIMEOUT", "900"))


@dataclass
class Result:
    ok: bool
    text: str
    session_id: str = ""
    cost_usd: float = 0.0
    denials: tuple[str, ...] = ()

    @property
    def denial_note(self) -> str:
        """acceptEdits 때문에 막힌 도구가 있으면 왜 멈췄는지 알려준다."""
        if not self.denials:
            return ""
        listed = ", ".join(f"`{d}`" for d in self.denials[:5])
        more = f" 외 {len(self.denials) - 5}건" if len(self.denials) > 5 else ""
        return f"\n\n🚫 권한이 없어 건너뛴 도구: {listed}{more}"


def run(prompt: str, cwd: str, session_id: str | None = None) -> Result:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--permission-mode", PERMISSION_MODE]
    if session_id:
        cmd += ["--resume", session_id]

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT
        )
    except FileNotFoundError:
        return Result(False, "`claude` 명령을 찾을 수 없습니다. Claude Code CLI를 설치해 주세요.")
    except subprocess.TimeoutExpired:
        return Result(False, f"{TIMEOUT}초 안에 끝나지 않아 중단했습니다.")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()[:1500]
        return Result(False, f"Claude 실행 실패 (exit {proc.returncode})\n```\n{detail}\n```")

    return Result(
        ok=not data.get("is_error"),
        text=data.get("result") or "(빈 응답)",
        session_id=data.get("session_id", ""),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        denials=tuple(_denial_names(data.get("permission_denials") or [])),
    )


def _denial_names(denials: list) -> list[str]:
    out = []
    for d in denials:
        name = d.get("tool_name") if isinstance(d, dict) else str(d)
        if name and name not in out:
            out.append(name)
    return out
