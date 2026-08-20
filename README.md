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

맥이 잠들면 노리도, 노리가 띄운 Remote Control 세션도 같이 멈춘다.
폰에서 계속 쓸 거면 유휴 슬립을 막고 띄운다.

```bash
caffeinate -i uv run nori.py
```

## 명령어

| 명령 | 하는 일 |
|---|---|
| `!projects` | 열 수 있는 프로젝트 목록 |
| `!sessions <프로젝트>` | 그 프로젝트의 Claude 세션 목록 |
| `!open <프로젝트>` | 그 프로젝트 폴더에서 Remote Control 세션을 띄운다 |
| `!live` | 지금 켜져 있는 Claude 세션 |
| `!stop <프로젝트>` | 노리가 띄운 세션을 닫는다 |
| `!trust` | 새 폴더 신뢰 확인이 걸려 있으면 승인 |
| `!clear` | 이 채널 대화 기억 삭제 |
| `!help` | 명령어 목록 |

```
!open nori
!sessions e-project
!stop nori
```

`!` `.` `/` 아무 걸로 시작해도 되고 한국어(`!목록` `!세션` `!열기` `!실행` `!종료` `!신뢰` `!초기화`)도 먹는다.

> `/clear` 처럼 슬래시로 쓰려면 Slack 앱 설정 → Slash Commands 에 직접 등록해야 한다
> (+ `commands` 스코프). 등록 안 된 슬래시 명령은 Slack이 삼켜버려서 봇까지 오지 않는다.

## Remote Control 요건

노리가 쓰는 건 **서버 모드** `claude remote-control --name <프로젝트>` 다.
터미널 UI를 쓰는 게 아니라 원격 연결을 기다리는 프로세스라서 노리 용도에 맞고,
자격이나 정책이 막히면 바로 종료하며 이유를 뱉는다.

켜지려면 아래가 모두 맞아야 한다. 하나라도 어긋나면 `!open` 이 이유를 그대로 알려준다.

- **요금제** — Pro / Max / Team / Enterprise. API 키 인증은 지원 안 됨
- **Team·Enterprise** — Owner가 [Claude Code 관리자 설정](https://claude.ai/admin-settings/claude-code)에서
  Remote Control 토글을 켜야 한다. 안 켜져 있으면
  `Remote Control is disabled by your organization's policy` 로 막힌다
- **로그인** — `claude` 실행 후 `/login` 으로 claude.ai 로그인
- **엔드포인트** — Bedrock / Vertex / Foundry 불가. `ANTHROPIC_BASE_URL` 이
  `api.anthropic.com` 이 아닌 곳을 가리키면 꺼진다
- **환경변수** — `DISABLE_TELEMETRY` `DO_NOT_TRACK`
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` `DISABLE_GROWTHBOOK` 중 하나라도 켜져 있으면 꺼진다
- **폴더 신뢰** — 그 폴더에서 `claude` 를 한 번은 직접 실행해 신뢰를 수락해둬야 한다.
  홈 디렉터리는 신뢰가 저장되지 않으므로 프로젝트 폴더에서 열어야 한다

`claude_runner.preflight()` 가 이 중 환경변수·홈디렉터리 조건을 미리 잡아낸다.
나머지는 실제로 띄워봐야 알 수 있어서, 세션 주소(`https://claude.ai/code/...`)가
나오는지로 성공을 판정한다. **세션이 떴다는 것만으로 성공 처리하지 않는다** —
`claude --remote-control` 은 Remote Control 이 실패해도 세션 자체는 그대로 뜨기 때문이다.

## 알아둘 것

- **세션 수명** — 노리가 띄운 세션은 노리의 pty에 매달려 있다. 노리를 끄면 같이 죽는다.
  오래 살려야 하면 터미널에서 `tmux`/`screen` 안에 직접 띄우는 게 낫다.
- **네트워크가 끊기면** — 서버 모드는 약 10분 뒤 프로세스가 종료된다. 다시 `!open` 하면 된다.
- **처음 여는 폴더** — Claude가 "이 폴더를 신뢰합니까?"를 묻고 멈춘다. `!trust` 로 승인하거나
  터미널에서 한 번 열어주면 그다음부터는 바로 열린다.
- **transcript는 첫 대화 이후에 생긴다** — 세션만 열고 아무 말도 안 하면 읽을 기록이 없다.
- **환경변수** — 노리를 Claude Code 세션 안에서 실행하면 `CLAUDE_CODE_CHILD_SESSION` 이 상속돼
  자식 세션의 transcript 저장이 꺼진다. `claude_runner` 가 `CLAUDE*` 를 전부 털고 띄운다.
