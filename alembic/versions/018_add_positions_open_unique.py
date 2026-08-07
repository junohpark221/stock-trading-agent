"""positions 부분 유니크 인덱스 — 계좌·종목당 open 포지션 1행 (PRJ-04 §2).

동일 종목 추가매수를 새 행으로 append하던 구조(F-27)를 MTS식 병합으로 바꾸면서,
`(account_id, symbol) WHERE status='open'` 부분 유니크 인덱스로 불변식을 DB에 못 박는다.
`PositionManager.merge_or_create`의 INSERT 경합 방어(FOR UPDATE + 유니크 충돌 재시도)가
이 인덱스를 전제로 동작한다. closed 행은 대상 밖이라 과거 이력은 그대로 남는다.

⚠️ 적용 전 중복 open 행이 남아 있으면 인덱스 생성이 실패한다(= 컨테이너 기동 실패).
   entrypoint.sh가 기동 시 `alembic upgrade head`를 자동 실행하므로 배포 전 확인할 것:

     SELECT account_id, symbol, COUNT(*) FROM positions
     WHERE status='open' GROUP BY 1,2 HAVING COUNT(*) > 1;

⚠️ 이 인덱스는 병합 로직(`merge_or_create`)과 반드시 같은 배포로 나가야 한다.
   인덱스만 먼저 나가면 다음 추가매수의 INSERT가 충돌해 체결 확정이 실패한다.

Revision ID: 018_positions_open_uniq
Revises: 017_security_group
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "018_positions_open_uniq"
down_revision: str | None = "017_security_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_positions_account_symbol_open"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "positions",
        ["account_id", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="positions")
