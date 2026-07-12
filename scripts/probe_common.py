"""PRJ-03 착수 게이트 프로브 공용 헬퍼.

4개 프로브 스크립트(probe_kis_investor_flow_api / probe_pykrx_coverage /
probe_kis_krx_crosscheck / probe_kis_flow_finalization)가 공유하는
KIS 클라이언트 구성·수급 TR 호출·리포트 출력 유틸.

리포트는 stdout(마크다운), 로그·경고는 stderr로 분리한다 —
EC2에서 `... exec app python scripts/<name>.py > report.md`로 수거하기 위함.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis

from src.broker.kis.client import KISClient
from src.config import get_settings
from src.data.cache import RedisCache

# ── PRJ-03 수급 TR 상수 ──────────────────────────────────────────────

INVESTOR_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
INVESTOR_DAILY_TR = "FHPTJ04160001"

MARKET_INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"
MARKET_INVESTOR_TR = "FHPTJ04040000"

SHORT_SALE_PATH = "/uapi/domestic-stock/v1/quotations/daily-short-sale"
SHORT_SALE_TR = "FHPST04830000"

LOAN_TRANS_PATH = "/uapi/domestic-stock/v1/quotations/daily-loan-trans"
LOAN_TRANS_TR = "HHPST074500C0"


# ── 로깅/클라이언트 부트스트랩 ───────────────────────────────────────


def setup_probe_logging() -> None:
    """structlog 출력을 stderr로 돌려 stdout 리포트를 오염시키지 않는다."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


async def build_kis_client() -> tuple[KISClient, Redis]:
    """.env(Settings) 자격증명으로 KISClient를 독립 구성한다.

    앱 lifespan 밖이므로 Redis/RedisCache를 직접 만들어 주입한다.
    반환된 (client, redis)는 close_kis_client()로 정리한다.
    """
    settings = get_settings()
    if settings.KIS_IS_PAPER and not settings.KIS_BASE_URL:
        print(
            "[경고] KIS_IS_PAPER=true — PRJ-03 수급 TR 4종은 모의 도메인 미지원으로"
            " 추정됨(모의계좌 Postman 컬렉션 부재). 호출 실패 시 이 자체가 게이트 ①"
            " 실측 결과이며, 실전 자격증명(.env)으로 재실행이 필요함.",
            file=sys.stderr,
        )
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    cache = RedisCache(redis)
    client = KISClient(settings=settings, cache=cache)
    await client.connect()
    return client, redis


async def close_kis_client(client: KISClient, redis: Redis) -> None:
    await client.disconnect()
    await redis.aclose()


# ── 수급 TR 호출 ─────────────────────────────────────────────────────


async def fetch_investor_daily(
    client: KISClient,
    symbol: str,
    anchor_date: str,
    *,
    max_pages: int = 12,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[int]]:
    """FHPTJ04160001 종목별 일별 투자자매매동향 — 연속조회(tr_cont) 페이징 수집.

    anchor_date(YYYYMMDD)를 단일 앵커로 과거 방향 시계열을 페이징한다.
    (output1, output2 누적 행, 페이지당 행수 리스트)를 반환.
    """
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": anchor_date,
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    }
    output1: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    rows_per_page: list[int] = []
    tr_cont = ""
    for _ in range(max_pages):
        data = await client._request(  # noqa: SLF001 — 프로브 전용 저수준 호출
            "GET", INVESTOR_DAILY_PATH, INVESTOR_DAILY_TR,
            params=params, tr_cont=tr_cont,
        )
        if not output1:
            output1 = data.get("output1") or {}
        page = data.get("output2") or []
        rows_per_page.append(len(page))
        rows.extend(page)
        if data.get("_tr_cont") in ("M", "F"):
            tr_cont = "N"
        else:
            break
    return output1, rows, rows_per_page


async def fetch_market_investor_daily(
    client: KISClient,
    *,
    market: str = "KSP",
    sector_code: str = "0001",
    anchor_date: str,
) -> dict[str, Any]:
    """FHPTJ04040000 시장/업종 단위 일별 투자자매매동향 — 단발 호출(원본 dict 반환)."""
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": sector_code,
        "FID_INPUT_DATE_1": anchor_date,
        "FID_INPUT_ISCD_1": market,
        "FID_INPUT_DATE_2": anchor_date,
        "FID_INPUT_ISCD_2": sector_code,
    }
    return await client._request(  # noqa: SLF001
        "GET", MARKET_INVESTOR_PATH, MARKET_INVESTOR_TR, params=params,
    )


# ── 날짜 유틸 (컨테이너는 UTC — 시장 판단은 전부 KST 명시) ──────────

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def recent_business_day(base: date | None = None) -> date:
    """주말만 배제한 최근 영업일(공휴일 미고려 — 프로브 앵커 용도로 충분). KST 기준."""
    d = base or now_kst().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def prev_business_day(base: date) -> date:
    return recent_business_day(base - timedelta(days=1))


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ── 리포트 출력 (stdout 마크다운) ────────────────────────────────────


def report_header(title: str) -> None:
    print(f"# {title}")
    print(f"\n- 실행 시각: {now_kst().isoformat(timespec='seconds')} (KST)")


def section(title: str) -> None:
    print(f"\n## {title}\n")


def kv(label: str, value: Any) -> None:
    print(f"- **{label}**: {value}")


def dump_fields(name: str, record: dict[str, Any] | None) -> None:
    """응답 레코드의 필드명 전수 + 샘플 값 덤프 (스키마 확정용 원자료)."""
    if not record:
        kv(name, "(빈 응답 — 필드 없음)")
        return
    print(f"\n### {name} — 필드 {len(record)}개\n")
    print("| # | field | sample value |")
    print("|---|-------|--------------|")
    for i, (k, v) in enumerate(record.items(), 1):
        print(f"| {i} | `{k}` | `{v}` |")


def sample_rows(rows: list[dict[str, Any]], n: int = 3) -> None:
    for row in rows[:n]:
        print(f"```json\n{json.dumps(row, ensure_ascii=False)}\n```")


def report_error(label: str, exc: Exception) -> None:
    kv(f"{label} 실패", f"`{type(exc).__name__}: {exc}`")
