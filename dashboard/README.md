# Grid Bot 대시보드

로컬 웹 UI로 그리드 봇의 **시세·주문·포지션·체결·PnL·백테스트**를 한눈에 봅니다.  
실주문은 **절대** 보내지 않으며, KIS는 시세/잔고 **조회만** 시도합니다(실패해도 파일 기반 오프라인 동작).

## 실행

```bash
# 편의 스크립트
/workspace/grid-bot/dashboard.sh

# 또는 직접
python3 /workspace/grid-bot/dashboard/server.py --port 8787
```

- 기본 URL: `http://127.0.0.1:8787/`
- JSON API: `http://127.0.0.1:8787/api/status`
- KIS 조회 생략: `/api/status?skip_kis=1` 또는 환경변수 `DASHBOARD_SKIP_KIS=1`

## 구성

| 파일 | 역할 |
|------|------|
| `server.py` | stdlib `http.server` — `/` + `/api/status` |
| `build_status.py` | `config.json`, `positions.json`, `orders_state.json`, `logs/*.jsonl`, 백테스트 CSV 집계 (+선택 KIS) |
| `index.html` | 다크 모던 UI, ~15초 JS 자동 갱신 |

## 화면 위젯

1. **Overview** — 심볼 367380, 현재가(KIS→없으면 로그/orders_state), 현금, 설정(0.2%/5레벨/TP 0.2%, 세션 09:05–15:20), LIVE 게이트
2. **오픈 주문** — `orders_state.json`
3. **포지션** — `positions.json`
4. **체결/트레이드** — 로그의 `buy_fill` / `sell_fill` / `broker.fill` / shadow·live 이벤트
5. **PnL** — 라운드트립 실현손익, 포지션 MTM, 백테스트 수익률(현재 파라미터 매칭)
6. **백테스트** — `backtest/results.csv`, `results_minute_10d.csv` 상위 행

## 안전

- `allow_mutations=False` — 주문/취소 API 호출 없음
- 시크릿·토큰·앱키 **출력/로그 금지**
- `LIVE_APPROVED` 존재·당일 유효 여부만 배지로 표시
