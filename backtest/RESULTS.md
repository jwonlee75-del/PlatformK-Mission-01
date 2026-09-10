# ETF 367380 그리드 봇 3개월 파라미터 그리드 서치 결과

## 데이터
- 종목: ACE 미국나스닥100 (`367380`)
- 기간: `20260608` ~ `20260908` (65 거래일)
- 소스: KIS `inquire_daily_itemchartprice` (env_dv=real, 일봉, 비가중조정 `fid_org_adj_prc=0`)
- Buy&Hold (시가→종가, 1주 기준): **-11.8564%**

## 시뮬레이션 가정
- 일별 시가 기준 `spacing = round_to_tick(open * spacing_pct)`, `tp = round_to_tick(open * tp_pct)` (tick=5, 반올림 half-up, **TP는 spacing과 독립**)
- 매수선: `open - i*spacing` (i=1..N), 수량 1
- `max_holdings = levels * 3` (N=5→15, N=10→30), `max_new_buys_per_day = levels`
- 일중 경로: 종가≥시가이면 O→L→H→C, 아니면 O→H→L→C (1원 단위 walk)
- 체결: 매수는 가격≤지정가, 매도는 가격≥지정가 (touch)
- 매도 체결 후 원래 매수가에 리사이클 매수 (일일 신규매수 카운트 **포함**); 래칫 재배치 체결은 카운트 **제외**
- 수수료: 왕복 0.03% (편도 0.015%), 세금: 양(+)의 (매도-매수) 금액의 15.4%
- 래칫: `day_high > top_buy_line + 2*spacing` 시 미체결 매수만 취소 후 1 spacing 상향
- 장 종료: 미체결 매수 취소, 포지션 유지, 종가 MTM
- 초기현금: `max_holdings * first_open * 1.05`

## Top 5 (total_return_pct 기준)

| 순위 | spacing_pct | levels | tp_pct | total_return% | realized_pnl | mtm_pnl | RTs | max_inv | max_DD% | ret/DD |
|------|-------------|--------|--------|---------------|--------------|---------|-----|---------|---------|--------|
| 1 | 0.006 | 10 | 0.004 | -2.6113% | 4875 | -33104 | 48 | 15 | 3.1721% | -0.8232 |
| 2 | 0.006 | 10 | 0.002 | -2.6396% | 2488 | -31023 | 54 | 13 | 2.9823% | -0.8851 |
| 3 | 0.006 | 10 | 0.006 | -2.8661% | 6924 | -37908 | 44 | 16 | 3.5358% | -0.8106 |
| 4 | 0.004 | 10 | 0.002 | -5.2044% | 4393 | -60655 | 96 | 25 | 5.9857% | -0.8695 |
| 5 | 0.006 | 5 | 0.004 | -5.2225% | 4875 | -33104 | 48 | 15 | 6.3319% | -0.8248 |

## 최고 기록
- **수익률 1위**: spacing=0.006, levels=10, tp=0.004 → return **-2.6113%**, realized=4875.02, max_DD=3.1721%, RTs=48, final_inv=13
- **실현손익 1위**: spacing=0.002, levels=10, tp=0.006 → realized **12579.58** (return -10.8271%)
- **위험조정 1위** (return / max(DD,0.01)): spacing=0.004, levels=10, tp=0.006 → RA **-0.7971** (return -5.7918%, DD 7.2661%)

## 권장 파라미터
- **권장**: `spacing_pct=0.006`, `levels=10`, `tp_pct=0.004`
- 근거: 18조합 중 total_return_pct 최고. max_holdings=30, max_new_buys_per_day=10
- 현재 `config.json`은 spacing=tp=0.002, levels=5 (TP=spacing 결합). 본 서치는 TP 독립. config 자동 변경은 하지 않음 — 적용 시 사용자 확인 필요.
- Buy&Hold -11.8564% 대비 그리드 수익률 -2.6113% (하락장에서 그리드가 매수 적립·부분 실현하는 구조).

## 주의사항 (Caveats)
1. **일봉 OHLC 경로 재구성은 실제 장중 체결보다 낙관적**입니다. O-L-H-C 가정이 고저점을 모두 터치한다고 보아 체결이 과대계상될 수 있습니다.
2. 슬리피지·호가 잔량·부분체결·주문 지연은 미반영.
3. 수수료/세금은 config 가정값(왕복 0.03%, 세금 15.4%)이며 계좌·상품별 실측과 다를 수 있습니다.
4. 하락 추세(-11.86%)에서 재고(final_inventory)와 MTM 손실이 결과에 크게 영향을 줍니다.
5. `max_holdings = levels * 3` 으로 N=5/10 공정 비교; 라이브 5레벨 봇의 고정 15와 N=5일 때 동일.
6. 라이브 엔진은 현재 TP=spacing 결합이나, 본 백테스트는 tp_pct 독립 스윕입니다.

## 파일
- OHLC: `ohlc_367380_3m.json` (65 rows)
- CSV: `results.csv` (18 rows, return 내림차순)
