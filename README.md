# Nori

Slack에서 말하면(폰에서 음성으로도) **GPT가 먼저 듣는** 비서.

노리는 코드를 직접 고치지 않는다. 원하는 프로젝트를 **Claude Remote Control 세션으로 열어주고**,
실제 작업은 사용자가 폰의 Claude 앱에서 그 세션을 몰아서 한다.
그리고 그 세션에서 무슨 일이 있었는지 노리가 읽어와 같이 이야기한다.

```
Slack 메시지 / 음성
      │  (음성이면 OpenAI STT로 받아쓰기)
      ▼
 GPT (LangGraph)
      ├── 잡담·질문 ─────────────▶ 그냥 답장
      ├── "e-project 열어줘" ────▶ claude --remote-control  ─▶ 폰 Claude 앱에서 작업
      └── "아까 뭐 했어?" ───────▶ ~/.claude/projects/*.jsonl 읽어서 요약
```

헤드리스(`claude -p`)를 쓰지 않는 이유: 승인 프롬프트가 뜨면 그냥 멈춘다.
Remote Control은 대화형이라 폰에서 승인을 누르면 된다.

대화 맥락은 채널별로 `nori.db`(SQLite)에 저장된다. **봇을 재시작해도 이어진다.**

## 구성

| 파일 | 역할 |
|---|---|
| `nori.py` | Slack Bolt 진입점 — 메시지·음성 수신, 명령어, 출력 |
| `brain.py` | LangGraph 그래프. GPT가 `open_project` / `read_work` 도구를 부른다 |
| `claude_runner.py` | pty로 `claude --remote-control` 실행, `claude agents --json` 조회 |
| `sessions.py` | `~/.claude/projects` 스캔 + transcript(jsonl)에서 최근 대화 추출 |
| `gpt.py` | OpenAI STT + 채팅 호출 래퍼 |

## 준비

```bash
cp .env.example .env   # 토큰 세 개 채우기
uv sync
```

Slack 앱 설정:

- **Socket Mode** ON, App-Level Token 에 `connections:write`
- Bot Token Scopes: `chat:write`, `files:read`, `im:history` (채널에서도 쓰려면 `channels:history`)
- Event Subscriptions: `message.im` (+ `message.channels`)

## 실행

```bash
uv run nori.py
```

## 명령어

| 명령 | 하는 일 |
|---|---|
| `!clear` | 이 채널 대화 기억 삭제 |
| `!help` | 명령어 목록 |
| `!projects` | Claude 프로젝트와 최근 세션 목록 |
| `!live` | 지금 켜져 있는 Claude 세션 |
| `!trust` | 새 폴더 신뢰 확인이 걸려 있으면 승인 |

`!` `.` `/` 아무 걸로 시작해도 되고 한국어(`!초기화` `!명령어` `!세션` `!실행` `!신뢰`)도 먹는다.

> `/clear` 처럼 슬래시로 쓰려면 Slack 앱 설정 → Slash Commands 에 직접 등록해야 한다
> (+ `commands` 스코프). 등록 안 된 슬래시 명령은 Slack이 삼켜버려서 봇까지 오지 않는다.

## 알아둘 것

- **세션 수명** — 노리가 띄운 Remote Control 세션은 노리의 pty에 매달려 있다.
  노리를 끄면 그 세션들도 같이 죽는다. 오래 살려야 하면 터미널에서 직접 띄우는 게 낫다.
- **처음 여는 폴더** — Claude가 "이 폴더를 신뢰합니까?"를 묻고 멈춘다. `!trust` 로 승인하거나
  터미널에서 한 번 열어주면 그다음부터는 바로 열린다.
- **transcript는 첫 대화 이후에 생긴다** — 세션만 열고 아무 말도 안 하면 읽을 기록이 없다.
- **환경변수** — 노리를 Claude Code 세션 안에서 실행하면 `CLAUDE_CODE_CHILD_SESSION` 이 상속돼
  자식 세션의 transcript 저장이 꺼진다. `claude_runner` 가 `CLAUDE*` 를 전부 털고 띄운다.
