# Grid Bot — KRX ETF 367380

페이퍼 시뮬레이션 + **게이트된** 실전 KIS 주문 배선. 기본 실행은 실주문을 보내지 않습니다.

## 심볼 / 규칙 요약 (`config.json`)

| 항목 | 값 |
|------|-----|
| 종목 | `367380` ACE 미국나스닥100 |
| 레벨 | 5, 주문당 1주 |
| 간격 | 일일 재계산 `round(price * 0.002 / 5) * 5` (틱 5) |
| 첫 매수선 | last − 1 spacing |
| 익절 | 매수가 + spacing → 체결 시 동일 매수가에 재매수(무제한) |
| 세션 | Asia/Seoul 09:05–15:20, 종료 시 **미체결 매수만** 취소 |
| 일일 신규매수 | 체결된 신규 매수만 ≤5 (래칫 재배치는 미포함) |
| 최대보유 | 15주; 도달 시 매도만 관리 |
| 래칫 | `day_high > top_buy + 2*spacing` → 미체결 매수만 취소 후 1 spacing 상향 재배치 |
| 손절 | 없음 |

## 실행 모드

### 1) Paper (기본, MockBroker)

```bash
cd /workspace/grid-bot
python3 main.py --mode paper --scenario demo
```

### 2) Live-dry (읽기 전용 KIS API — **주문 없음**)

자격증명: `/home/box/KIS/config/kis_devlp.yaml` + 로컬 `open-trading-api` `kis_auth`.

```bash
cd /workspace/grid-bot
python3 main.py --mode live-dry
```

동작:
- `auth` → `inquire_price(367380)` → `inquire_balance`
- 현재가 기준 spacing / 5개 매수선 출력
- `submit` 시도 시 게이트로 거부 (증명용)
- 시크릿/토큰 **출력 안 함**

### 3) Live bootstrap (실주문 — **이중 승인 필수**)

아래 **둘 다** 필요합니다.

1. `--mode live`
2. `--i-approve-live-orders` **또는** 파일 `/workspace/grid-bot/LIVE_APPROVED`에 **오늘 서울 날짜** `YYYY-MM-DD`

승인 없이 bootstrap 하면 거부되고 exit ≠ 0, 주문 없음.

```bash
# (권장) 플래그로 승인
cd /workspace/grid-bot
python3 main.py --mode live --bootstrap-grid --i-approve-live-orders

# 또는 파일 승인 (서울 날짜)
echo "$(TZ=Asia/Seoul date +%F)" > /workspace/grid-bot/LIVE_APPROVED
python3 main.py --mode live --bootstrap-grid
```

`bootstrap-grid`: 현재가 기준 매수 지정가 5건을 **한 번** 넣고 `orders_state.json` / `positions.json` 메타에 주문 ID를 저장한 뒤 종료합니다.  
의도 주문은 제출 전 `logs/live-intent-*.jsonl`에 기록됩니다.

### 4) Live session dry-loop (시세 폴링 + ShadowBroker — **실주문 없음**)

실주문 없이 세션 루프 전체를 검증합니다. 라이브 `inquire_price`로 가격을 읽고, 엔진은 `ShadowBroker`로만 의도 주문을 기록합니다.

```bash
cd /workspace/grid-bot
python3 main.py --mode live --run-session --dry-loop --max-ticks 10 --interval 3
# 동의어:
python3 main.py --mode live-session-dry --max-ticks 10 --interval 3
```

동작:
- 잔고 1회 + 시세 N회 폴링 (`--interval` 초)
- `GridEngine.on_price` → 그리드/래칫/체결(섀도우) 결정 출력
- 세션 밖(장전/장후) + `--max-ticks` 이면 **가상 세션 시계**(09:05부터)로 엔진 결정을 시연
- 종료 시 미체결 매수 would-cancel 로그 (`shadow_cancel`)
- `orders_state.json` / `positions.json` 갱신
- API 실패 시 텔레그램 스텁(로그; `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` 있으면 전송, 토큰 미출력)

### 5) Live session (실주문 — **이중 승인 필수**)

```bash
# 사용자 승인 후에만. 검증 시 실행하지 말 것.
python3 main.py --mode live --run-session --i-approve-live-orders --interval 5
```

실경로: `LiveKISBroker` 제출/취소 + `poll_fills_via_daily_ccld` 체결 동기화 + 15:20 미체결 매수 취소.

## 안전 규칙

| 규칙 | 내용 |
|------|------|
| 기본 | mutations 비허용 (`allow_mutations=False`) |
| live-dry | 주문/취소 불가, 시세·잔고만 |
| live | CLI 승인 **또는** `LIVE_APPROVED` (당일 서울 날짜) |
| 시크릿 | 로그/콘솔에 앱키·시크릿·토큰 출력 금지 |

## 패키지 구조

```
grid-bot/
  config.json
  models.py / state_machine.py / session.py / persistence.py
  broker_mock.py       # MockBroker + ShadowBroker; LiveKIS 재수출
  broker_kis.py        # LiveKISBroker (order_cash / rvsecncl / inquire_*)
  grid_engine.py
  main.py
  orders_state.json    # live bootstrap 후 생성
  positions.json
  logs/run-*.jsonl
  logs/live-intent-*.jsonl
  LIVE_APPROVED        # 선택: 당일 YYYY-MM-DD
```

**Engine vs Broker:** `GridEngine`은 전략만, `BrokerAdapter`가 주문. Paper=`MockBroker`, Live=`LiveKISBroker`.

## 체결 폴링

`LiveKISBroker.poll_fills_via_daily_ccld()`가 당일 `inquire_daily_ccld`(체결)로 ODNO를 매칭해 `FILLED`로 전이합니다.  
비-dry `--run-session`에서 틱마다 호출됩니다. dry-loop는 `ShadowBroker.match_on_price`로 시연합니다.

## 의존성

Paper: Python 3 stdlib.  
Live / live-dry / dry-loop: `open-trading-api/examples_llm` + `pandas`, `PyYAML`, `requests`, `pycryptodome`, `websockets`.

## 남은 TODO

- 래칫 실주문 경로의 장중 검증 (승인 후)
- 텔레그램 알림 실계정 연동 확인
- bootstrap 직후 `--run-session` 이어붙이기(orders_state ODNO 복원 — 구조는 구현됨)
