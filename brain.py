"""노리의 머리 — 모든 말을 GPT가 먼저 읽는다.

노리는 직접 코딩하지 않는다. 하는 일은 셋이다.
  1. 그냥 대화
  2. 원하는 프로젝트를 Claude Remote Control 세션으로 열어주기 (실제 작업은 Claude 앱에서)
  3. 그 세션에서 무슨 일이 있었는지 읽어와서 같이 이야기하기

대화 맥락은 LangGraph 체크포인터(SQLite)에 채널별로 저장되므로 봇을 재시작해도 이어진다.
"""

from __future__ import annotations

import os
import sqlite3
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
MAX_TOOL_ROUNDS = 4     # 도구 호출 무한루프 방지


# ------------------------------------------------------------ Slack 출력 통로

@dataclass
class Sink:
    say: Callable[[str], str]      # 메시지 보내기 → ts


_sinks: dict[str, Sink] = {}


def bind(channel: str, sink: Sink) -> None:
    _sinks[channel] = sink


# ------------------------------------------------------------------ 도구 정의

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_project",
            "description": (
                "지정한 프로젝트를 Claude Remote Control 세션으로 연다. 열고 나면 사용자가 "
                "폰의 Claude 앱에서 직접 그 세션을 몰고 작업한다. 사용자가 '열어줘', '켜줘', "
                "'작업하고 싶어' 라고 할 때 부른다. 네가 코드를 고치는 게 아니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "프로젝트 절대경로. 아래 목록에 적힌 그대로.",
                    }
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_work",
            "description": (
                "Claude 세션에서 최근에 무슨 작업이 있었는지 읽어온다. 사용자가 '아까 뭐 했어?', "
                "'그 작업 어떻게 됐어?', '뭐 고쳤어?' 처럼 물으면 부른다. 결과를 받아 네가 "
                "사람 말로 정리해서 답한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "세션 id 앞 8자. 모르면 프로젝트의 가장 최근 세션을 쓰도록 빈 값.",
                    },
                    "project": {
                        "type": "string",
                        "description": "프로젝트 절대경로. session 을 모를 때 필요하다.",
                    },
                    "turns": {
                        "type": "integer",
                        "description": "가져올 최근 대화 수. 기본 12.",
                    },
                },
                "required": [],
            },
        },
    },
]

COMMANDS = {
    "/clear": "이 채널의 대화 기억을 지운다",
    "/help": "쓸 수 있는 명령어 목록",
    "/projects": "Claude 프로젝트와 최근 세션 목록",
    "/live": "지금 켜져 있는 Claude 세션",
    "/trust": "새 폴더 신뢰 확인이 걸려 있으면 승인",
}


# ------------------------------------------------------------------ 시스템 프롬프트

def catalog(projects: int = 12, per_project: int = 5) -> str:
    lines = []
    for cwd, count, _ in sessions.projects(limit=projects):
        lines.append(f"### {cwd}  (세션 {count}개)")
        for s in sessions.sessions_for(cwd, limit=per_project):
            lines.append(f"- {s.sid[:8]} · {s.label(60)}")
    return "\n".join(lines) or "(아직 세션 기록 없음)"


def live_list() -> str:
    rows = claude_runner.agents()
    if not rows:
        return "(켜져 있는 세션 없음)"
    return "\n".join(
        f"- {a.get('name','?')} · {a.get('sessionId','')[:8]} · {a.get('cwd','')}"
        for a in rows
    )


def system_prompt() -> str:
    cmds = "\n".join(f"- `{k}` — {v}" for k, v in COMMANDS.items())
    return f"""너는 '노리'다. 사용자의 Mac에 붙어 있는 Slack 비서이고, 한국어로 짧고 편하게 답한다.

**너는 코드를 직접 고치지 않는다.** 실제 작업은 사용자가 폰의 Claude 앱에서 Remote Control로 한다.
네 역할은 셋이다.
1. 그냥 대화하고 질문에 답하기
2. 사용자가 원하는 프로젝트를 open_project 로 열어주기
3. 그 세션에서 무슨 일이 있었는지 read_work 로 읽어와 같이 이야기하기

규칙:
- 사용자가 말한 이름이 아래 목록의 프로젝트 하나와 분명히 맞으면 되묻지 말고 바로 연다.
- 후보가 둘 이상이라 진짜 헷갈릴 때만 후보를 대며 되묻는다.
- read_work 결과는 그대로 뱉지 말고, 뭘 했는지 사람 말로 3~5줄로 정리해라.
- 잡담이나 일반 지식 질문에는 도구를 부르지 않는다.
- 도구를 부를 때 content 는 비워라.

이 채널의 대화는 파일에 저장돼서 계속 기억한다. 봇을 재시작해도 이어진다.
`/clear` 전까지는 지워지지 않으니 "기억 못 한다"는 말은 하지 마라.

쓸 수 있는 명령어(사용자가 물어보면 알려준다):
{cmds}

지금 켜져 있는 Claude 세션:
{live_list()}

이 Mac의 Claude 프로젝트와 최근 세션:
{catalog()}
"""


# ---------------------------------------------------------------- 도구 실행

def _resolve_project(name: str) -> str | None:
    known = [cwd for cwd, _, _ in sessions.projects(limit=1000)]
    if name in known:
        return name
    name = name.rstrip("/")
    for cwd in known:
        if cwd.rstrip("/").endswith(name) or os.path.basename(cwd) == os.path.basename(name):
            return cwd
    return None


def _open_project(channel: str, project: str) -> str:
    cwd = _resolve_project(project)
    if cwd is None:
        return f"'{project}' 라는 프로젝트를 목록에서 못 찾았다. 사용자에게 다시 물어봐라."

    res = claude_runner.launch(cwd)
    if not res.ok:
        return f"열기 실패. {res.message}"
    return (f"열었다. 프로젝트 {cwd}, 세션 {res.session_id[:8] or '(확인중)'}, "
            f"이름 {res.name}. {res.message} "
            f"사용자에게 Claude 앱에서 이 세션을 잡으면 된다고 알려줘라.")


def _read_work(channel: str, session: str = "", project: str = "", turns: int = 12) -> str:
    sid = (session or "").strip()
    if not sid:
        cwd = _resolve_project(project) if project else None
        if cwd is None:
            return "어느 프로젝트인지 몰라서 못 읽었다. 사용자에게 물어봐라."
        recent = sessions.sessions_for(cwd, limit=1)
        if not recent:
            return f"{cwd} 에 세션 기록이 없다."
        sid = recent[0].sid

    body = sessions.recent_turns(sid, limit=max(4, min(int(turns or 12), 30)))
    if not body:
        return f"세션 {sid[:8]} 의 기록을 찾지 못했다."
    return f"세션 {sid[:8]} 의 최근 대화:\n{body}"


DISPATCH = {"open_project": _open_project, "read_work": _read_work}


# -------------------------------------------------------------------- 그래프

class State(MessagesState):
    channel: str


def _trim(messages: list) -> list:
    cut = messages[-HISTORY_LIMIT:]
    while cut and isinstance(cut[0], ToolMessage):
        cut.pop(0)
    return cut


_llm = None


def _model():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(model=MODEL).bind_tools(TOOLS)
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
        fn = DISPATCH.get(call["name"])
        try:
            text = fn(state["channel"], **call["args"]) if fn else f"모르는 도구: {call['name']}"
        except Exception as exc:
            text = f"도구 실행 중 오류: {exc}"
        out.append(ToolMessage(content=text, tool_call_id=call["id"]))
    return {"messages": out}


def _route(state: State) -> str:
    last = state["messages"][-1]
    if not (isinstance(last, AIMessage) and last.tool_calls):
        return END
    rounds = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
    return "act" if rounds <= MAX_TOOL_ROUNDS else END


_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_saver = SqliteSaver(_conn)

_builder = StateGraph(State)
_builder.add_node("agent", _agent)
_builder.add_node("act", _act)
_builder.add_edge(START, "agent")
_builder.add_conditional_edges("agent", _route, {"act": "act", END: END})
_builder.add_edge("act", "agent")     # 도구 결과를 보고 노리가 사람 말로 정리한다
graph = _builder.compile(checkpointer=_saver)


# ---------------------------------------------------------------- 바깥 인터페이스

def handle(channel: str, text: str) -> None:
    graph.invoke(
        {"messages": [HumanMessage(text)], "channel": channel},
        config={"configurable": {"thread_id": channel}, "recursion_limit": 20},
    )


def clear(channel: str) -> None:
    _saver.delete_thread(channel)
