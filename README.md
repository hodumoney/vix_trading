# VIX 적립 매수 시스템 (자동 수집 + 매일 텔레그램 알림)

VIX(공포지수)에 따라 매일 매수금액(1만~10만원)을 계산하는 다기간 DCA 함수를,
GitHub Actions로 
(1) 매 거래일 데이터 자동 수집·재적합, 
(2) 매일 밤 11시 매수 신호를
텔레그램으로 1회 발송하는 개인용 시스템입니다.

- 함수: 다기간(1·2·3·5년) 기대성장 곡선, 상한 앵커 VIX 45
- 매수 기준: 전일 종가 VIX로 계산(곡선과 일치), 현재 장중 VIX는 참고 표시
- 발송: Telegram Bot -> 내 텔레그램
- 휴장일: 매수금액 대신 VIX만 안내
- 주문: 사람이 토스에서 직접 (자동주문 없음)

## 파일

| 파일 | 역할 |
|---|---|
| data.json | 곡선 충분통계량 + 기간별 대기버퍼 (저장소가 곧 DB) |
| update.py | 매 거래일 VIX/QLD 종가 수집 -> data.json 갱신 (Stooq->Yahoo 폴백) |
| notify.py | 매일 밤 11시 매수 신호 텔레그램 발송 |
| index.html | GitHub Pages 대시보드 (오늘 금액/곡선/데이터 이력) |
| .github/workflows/daily.yml | 데이터 수집 스케줄 |
| .github/workflows/alert.yml | 텔레그램 알림 스케줄 (KST 23:00) |

## 설치 (아래 본문 안내를 그대로 따라오세요)

1) 저장소에 파일 전부 업로드
2) Settings -> Actions -> General -> Workflow permissions = Read and write
3) Settings -> Pages -> Deploy from a branch, main / (root)  (대시보드)
4) Settings -> Secrets and variables -> Actions 에 2개 등록:
     TG_TOKEN     BotFather가 준 봇 토큰
     TG_CHAT_ID   내 chat id (@userinfobot으로 확인)
5) Actions -> daily-vix-update -> Run workflow (데이터 한 번 수집)
   Actions -> daily-buy-alert -> Run workflow (테스트 알림 즉시 발송)

## 매일 일어나는 일
1. 미국 장 마감 후: update.py가 어제 종가 수집 -> data.json 커밋 -> 곡선 재적합
2. KST 23:00: notify.py가 전일 종가 VIX로 매수금액 계산 + 장중 VIX/QLD/환율 참고 -> 텔레그램 발송
3. 사용자는 알림 보고 토스에서 QLD 매수

## 알림 예시
    [VIX 적립] 07/10 신호
    매수 기준: 전일 종가 VIX 16.1 · 평온
    오늘 매수: 14,000원 (약 0.11주 @ $90.3, 환율 1,380원)
    참고: 현재 장중 VIX 16.8
    -> 토스에서 QLD를 위 금액만큼 매수

## 주의
- 발송 시각(KST 23:00) = 미국 장 개장 직후. 전일 종가는 이미 확정, 장중 VIX는 참고용.
- Stooq/Yahoo는 무료·비공식 소스라 드물게 실패 가능. 실시간 조회가 모두 막히면
  평일 기준으로 개장을 추정해 신호를 보냅니다.
- 봇 토큰은 코드/커밋에 직접 넣지 말고 Secrets에만 두세요.
- 참고용 도구이며, 투자 판단과 결과의 책임은 본인에게 있습니다.

---

# VIX 30 돌파 단기매매 알림 (추가)

호두머니 Q투자법 연구(2000~2026 백테스트)에서 고른 규칙을 텔레그램으로 알려줍니다.
기존 VIX 적립 알림과 별개로 돌아가며, 같은 텔레그램 봇(TG_TOKEN, TG_CHAT_ID)을 씁니다.

## 규칙
1. 오늘 VIX 종가 30 이상, 어제 종가 30 미만 → 신호
2. 다음 미국 거래일 시가에 매수 (기본 TQQQ)
3. 2거래일 뒤 시가에 매도
4. 보유 마지막 날 종가에 다시 30을 넘으면 팔지 않고 2일 연장

## 오는 알림
| 알림 | 언제 |
|---|---|
| 🚨 매수 신호 | 돌파한 날 다음 한국시간 아침 (예: 금요일 돌파 → 토요일 아침) |
| ⏰ 오늘 밤 매수 | 신호가 주말을 끼어 매수일과 날짜가 다를 때, 매수일 아침 |
| ↪️ 보유 연장 | 보유 마지막 날 다시 돌파했을 때, 다음 아침 |
| 🔔 오늘 매도 | 매도해야 하는 날 아침 |

알림이 없는 날은 아무것도 오지 않습니다.

## 파일
| 파일 | 역할 |
|---|---|
| signal30.py | VIX 종가 수집(Yahoo → Stooq → CBOE) + 규칙 판단 + 텔레그램 발송 |
| signal30_state.json | 이미 보낸 알림 기록(중복 방지). Actions가 자동 커밋 |
| .github/workflows/vix30-alert.yml | 매일 한국시간 07:40 실행, 10:10 재확인 |

## 테스트
Actions → vix30-alert → Run workflow → mode = status → 지금 상태가 텔레그램으로 옵니다.

## 바꾸고 싶을 때
- ETF: Settings → Secrets and variables → Actions → Variables 탭 → `VIX30_ETF` = QLD (없으면 TQQQ)
- 휴장일: signal30.py 위쪽 HOLIDAYS에 해마다 NYSE 휴장일 추가 (현재 2025~2028 입력됨)

## 주의
- 매수·매도 판단은 VIX 종가 기준이라 장중 값과 다를 수 있습니다.
- 주문은 사람이 토스에서 직접 합니다(자동주문 없음).
- 과거 백테스트 결과는 미래를 보장하지 않습니다. 참고용 도구입니다.
