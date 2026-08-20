# Nori

Slack에서 말하면(폰에서 음성으로도) **GPT가 먼저 듣고** 판단한다.
그냥 대화면 노리가 일반 메시지로 답하고, 실제 작업이 필요하면 Claude Code를
특정 프로젝트·세션에서 깨워 **그 건만 스레드(댓글)로** 진행한다.

```
Slack 메시지 / 음성
        │  (음성이면 OpenAI STT로 받아쓰기)
        ▼
   GPT (LangGraph)   ── 잡담·질문 ─────────▶ 노리가 일반 메시지로 답장
        │
        └─ 작업이 필요하다 ──▶ claude -p --resume  ──▶ 시작 알림(일반 메시지)
                                                      └─ 결과는 그 밑 스레드에
```

대화 맥락은 채널별로 `nori.db`(SQLite)에 저장된다. **봇을 재시작해도 이어진다.**

Claude 결과가 길면 Slack 한도(UTF-8 바이트 기준)에 맞춰 여러 덩어리로 쪼개 스레드에 이어 붙이고,
원문은 항상 `runs/<세션>-<시각>.md` 로 통째로 남긴다.

## 구성

| 파일 | 역할 |
|---|---|
| `nori.py` | Slack Bolt 진입점 — 메시지·음성 수신, 명령어, Slack 출력 |
| `brain.py` | LangGraph 그래프. GPT가 라우팅하고 `run_claude` 도구를 부른다 |
| `sessions.py` | `~/.claude/projects` 를 훑어 프로젝트·세션 목록 생성 |
| `claude_runner.py` | `claude -p --resume` 헤드리스 실행 |
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
| `!mode` | 현재 Claude 권한 모드 |

`!` `.` `/` 아무 걸로 시작해도 되고 한국어(`!초기화`, `!명령어`, `!세션`, `!권한`)도 먹는다.

> `/clear` 처럼 슬래시로 쓰려면 Slack 앱 설정에서 슬래시 명령을 직접 등록해야 한다
> (Slash Commands 에 `/clear` `/help` `/projects` `/mode` 추가 + `commands` 스코프).
> 등록 안 했으면 Slack이 "유효하지 않은 명령"이라며 삼켜버리니 `!clear` 를 쓰면 된다.

그 밖엔 그냥 말하거나 음성 보내면 된다. 어느 프로젝트인지 애매하면 노리가 되묻는다.

## 권한

기본 `acceptEdits` — 파일 편집은 자동 승인, `Bash` 같은 도구는 막힌다.
막혀서 건너뛴 도구가 있으면 결과 메시지에 같이 뜬다.
전부 통과시키려면 `.env` 에 `NORI_PERMISSION_MODE=bypassPermissions` (신뢰하는 폴더에서만).
