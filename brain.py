"""노리의 머리 — GPT가 모든 말을 먼저 읽고, 필요할 때만 Claude를 깨운다.

대화 맥락은 LangGraph 체크포인터(SQLite)에 채널별로 저장되므로 봇을 재시작해도 이어진다.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

import claude_runner
import sessions

DB_PATH = os.environ.get("NORI_DB", str(Path(__file__).parent / "nori.db"))
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
HISTORY_LIMIT = 30      # LLM에 넣을 최근 메시지 수


# ------------------------------------------------------------ Slack 출력 통로

@dataclass
class Sink:
    """brain 이 Slack에 글을 쓰는 통로. nori.py가 구현해서 넘긴다."""
    say: Callable[[str], str]              # 일반 메시지(댓글 아님) → ts
    reply: Callable[[str, str], str]       # (부모 ts, 본문) → ts
    update: Callable[[str, str], None]     # (ts, 본문)
    result: Callable[..., None]            # (부모 ts, 갱신할 ts, 본문, 원문경로) — 길면 쪼개 보낸다


_sinks: dict[str, Sink] = {}


def bind(channel: str, sink: Sink) -> None:
    _sinks[channel] = sink


# ------------------------------------------------------------------ 도구 정의

RUN_CLAUDE = {
    "type": "function",
    "function": {
        "name": "run_claude",
        "description": (
            "사용자의 Mac에서 Claude Code를 실제로 돌린다. 코드 수정·파일 조사·명령 실행처럼 "
            "실제 작업이 필요할 때만 호출한다. 어느 프로젝트인지 확실하지 않으면 호출하지 말고 "
            "먼저 사람에게 되물어라. 잡담이나 일반 질문에는 절대 호출하지 않는다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트 절대경로. 아래 목록에 적힌 그대로 쓴다.",
                },
                "session": {
                    "type": "string",
                    "description": "이어붙일 세션 id의 앞 8자. 새 세션으로 시작하려면 'new'.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Claude에게 그대로 전달할 지시문. 반드시 한국어로, 사용자의 의도를 살려 구체적으로.",
                },
            },
            "required": ["project", "session", "prompt"],
        },
    },
}

COMMANDS = {
    "/clear": "이 채널의 대화 기억을 지운다",
    "/help": "쓸 수 있는 명령어 목록",
    "/projects": "Claude 프로젝트와 최근 세션 목록",
    "/mode": "현재 Claude 권한 모드 확인",
}


def catalog(projects: int = 12, per_project: int = 6) -> str:
    lines = []
    for cwd, count, _ in sessions.projects(limit=projects):
        lines.append(f"### {cwd}  (세션 {count}개)")
        for s in sessions.sessions_for(cwd, limit=per_project):
            lines.append(f"- {s.sid[:8]} · {s.label(60)}")
    return "\n".join(lines) or "(아직 세션 기록 없음)"


def system_prompt() -> str:
    cmds = "\n".join(f"- `{k}` — {v}" for k, v in COMMANDS.items())
    return f"""너는 '노리'다. 사용자의 Mac에 붙어 있는 Slack 비서이고, 한국어로 편하게 반말 섞어 짧게 답한다.

이 채널의 대화는 파일에 저장돼서 계속 기억한다. 봇을 재시작해도 이어진다.
사용자가 `/clear` 를 치기 전까지는 지워지지 않으니, "기억 못 한다"는 말은 하지 마라.

사용자가 하는 모든 말(음성 포함)을 네가 먼저 읽는다. 판단은 둘 중 하나다.

1. 그냥 대화·질문·설명 요청 → 네가 직접 답한다. 도구를 부르지 않는다.
2. Mac에서 실제 작업이 필요하다(코드 고치기, 파일 뒤지기, 뭔가 만들기) → run_claude 를 부른다.

run_claude 를 부를 때 규칙:
- 사용자가 말한 이름이 아래 목록의 프로젝트 하나와 분명히 맞으면 되묻지 말고 바로 부른다.
  (예: "노리", "nori" → 아래 목록에서 폴더 이름이 nori 인 경로)
- 후보가 둘 이상이라 진짜 헷갈릴 때만 후보를 대며 되묻는다.
- 사용자가 "아까 그거", "그 세션" 처럼 말하면 대화 기록을 보고 판단한다.
- 이어갈 이유가 뚜렷하지 않으면 session 은 'new'.
- prompt 는 반드시 한국어로 쓴다.
- 도구를 부를 때 content 는 비워라. 시작 알림은 시스템이 따로 보낸다.

쓸 수 있는 명령어(사용자가 물어보면 이걸 알려준다):
{cmds}

현재 Mac의 Claude Code 프로젝트와 최근 세션:
{catalog()}
"""


# ---------------------------------------------------------------- 실행 로직

def _resolve_project(name: str) -> str | None:
    known = [cwd for cwd, _, _ in sessions.projects(limit=1000)]
    if name in known:
        return name
    name = name.rstrip("/")
    for cwd in known:
        if cwd.rstrip("/").endswith(name) or os.path.basename(cwd) == os.path.basename(name):
            return cwd
    return None


def _resolve_session(cwd: str, ref: str) -> str | None:
    ref = (ref or "").strip()
    if not ref or ref.lower() in {"new", "새", "새로", "none"}:
        return None
    for s in sessions.sessions_for(cwd, limit=1000):
        if s.sid.startswith(ref):
            return s.sid
    return None


RUNS_DIR = Path(os.environ.get("NORI_RUNS", Path(__file__).parent / "runs"))


def _save_run(cwd: str, prompt: str, res) -> str:
    """Slack에 다 못 실은 원문을 통째로 남겨둔다."""
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        path = RUNS_DIR / f"{res.session_id[:8] or 'unknown'}-{int(time.time())}.md"
        path.write_text(
            f"# {cwd}\n\n## 지시\n{prompt}\n\n## 결과\n{res.text}\n", encoding="utf-8"
        )
        return str(path)
    except OSError:
        return ""


def _run_claude(channel: str, project: str, session: str, prompt: str) -> str:
    sink = _sinks.get(channel)
    if sink is None:
        return "Slack 출력 통로가 없어 실행하지 못했다."

    cwd = _resolve_project(project)
    if cwd is None:
        sink.say(f"`{project}` 라는 프로젝트를 못 찾겠어. 다시 말해줄래?")
        return f"프로젝트 '{project}' 를 찾지 못해 실행하지 않았다."

    sid = _resolve_session(cwd, session)
    tag = f"`{sid[:8]}` 세션에 이어서" if sid else "새 세션으로"
    parent = sink.say(f"📂 `{cwd}`\n🧵 {tag} 시작할게.")
    running = sink.reply(parent, "⏳ Claude 작업 중…")

    res = claude_runner.run(prompt, cwd=cwd, session_id=sid)
    raw = _save_run(cwd, prompt, res)
    head = "✅" if res.ok else "⚠️"
    foot = f"\n\n_${res.cost_usd:.3f} · `{res.session_id[:8]}`_"
    sink.result(parent, running, f"{head} {res.text}{res.denial_note}{foot}", raw)

    status = "성공" if res.ok else "실패"
    return f"Claude 실행 {status}. 프로젝트 {cwd}, 세션 {res.session_id[:8]}. 결과 요약: {res.text[:400]}"


# -------------------------------------------------------------------- 그래프

class State(MessagesState):
    channel: str


def _trim(messages: list) -> list:
    """토큰이 무한정 늘지 않게 자르되, 짝 잃은 ToolMessage 로 시작하지 않게 한다."""
    cut = messages[-HISTORY_LIMIT:]
    while cut and isinstance(cut[0], ToolMessage):
        cut.pop(0)
    return cut


_llm = None


def _model():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(model=MODEL).bind_tools([RUN_CLAUDE])
    return _llm


def _agent(state: State) -> dict:
    answer = _model().invoke([SystemMessage(system_prompt())] + _trim(state["messages"]))
    if answer.content and (sink := _sinks.get(state["channel"])):
        sink.say(str(answer.content))
    return {"messages": [answer]}


def _act(state: State) -> dict:
    last = state["messages"][-1]
    out = []
    for call in getattr(last, "tool_calls", []):
        text = _run_claude(state["channel"], **call["args"])
        out.append(ToolMessage(content=text, tool_call_id=call["id"]))
    return {"messages": out}


def _route(state: State) -> str:
    last = state["messages"][-1]
    return "act" if isinstance(last, AIMessage) and last.tool_calls else END


_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_saver = SqliteSaver(_conn)

_builder = StateGraph(State)
_builder.add_node("agent", _agent)
_builder.add_node("act", _act)
_builder.add_edge(START, "agent")
_builder.add_conditional_edges("agent", _route, {"act": "act", END: END})
_builder.add_edge("act", END)   # Claude 결과는 스레드에 직접 붙었으니 다시 LLM을 태우지 않는다
graph = _builder.compile(checkpointer=_saver)


# ---------------------------------------------------------------- 바깥 인터페이스

def handle(channel: str, text: str) -> None:
    graph.invoke(
        {"messages": [HumanMessage(text)], "channel": channel},
        config={"configurable": {"thread_id": channel}},
    )


def clear(channel: str) -> None:
    _saver.delete_thread(channel)
