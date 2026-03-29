"""Phase 8 Step 1: Account 모델, AccountCrypto, AccountStatus enum, config 테스트.

Tests:
- AccountStatus enum 직렬화/역직렬화
- AccountCrypto Fernet 암호화/복호화 왕복
- Account ORM 모델 구조 (컬럼, PK, 기본값)
- Config ACCOUNT_ENCRYPTION_KEY 환경변수
- 기존 7개 모델에 account_id 컬럼 존재 확인
- PortfolioSnapshot unique constraint 변경 확인
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.core.enums import AccountStatus
from src.db.models.account import Account, AccountCrypto
from src.db.models.execution import ApprovalRequestDB, Order
from src.db.models.llm import DecisionLog
from src.db.models.scheduler import JobExecution
from src.db.models.strategy import AgentMemory, PortfolioSnapshot, PositionRecord
from tests.conftest import make_settings


# ---------------------------------------------------------------------------
# AccountStatus Enum
# ---------------------------------------------------------------------------


class TestAccountStatus:
    """AccountStatus enum 테스트."""

    def test_values(self):
        assert AccountStatus.ACTIVE == "active"
        assert AccountStatus.INACTIVE == "inactive"
        assert AccountStatus.SUSPENDED == "suspended"

    def test_from_string(self):
        assert AccountStatus("active") is AccountStatus.ACTIVE
        assert AccountStatus("inactive") is AccountStatus.INACTIVE
        assert AccountStatus("suspended") is AccountStatus.SUSPENDED

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            AccountStatus("unknown")

    def test_all_members(self):
        assert len(AccountStatus) == 3


# ---------------------------------------------------------------------------
# AccountCrypto
# ---------------------------------------------------------------------------


class TestAccountCrypto:
    """Fernet 암호화/복호화 테스트."""

    @pytest.fixture
    def fernet_key(self) -> str:
        return Fernet.generate_key().decode()

    def test_encrypt_decrypt_roundtrip(self, fernet_key: str):
        plaintext = "my_secret_app_key_12345"
        encrypted = AccountCrypto.encrypt(plaintext, fernet_key)
        assert encrypted != plaintext
        decrypted = AccountCrypto.decrypt(encrypted, fernet_key)
        assert decrypted == plaintext

    def test_decrypt_wrong_key(self, fernet_key: str):
        other_key = Fernet.generate_key().decode()
        encrypted = AccountCrypto.encrypt("secret", fernet_key)
        with pytest.raises(InvalidToken):
            AccountCrypto.decrypt(encrypted, other_key)

    def test_encrypt_empty_string(self, fernet_key: str):
        encrypted = AccountCrypto.encrypt("", fernet_key)
        assert AccountCrypto.decrypt(encrypted, fernet_key) == ""

    def test_invalid_key_raises(self):
        with pytest.raises(Exception):
            AccountCrypto.encrypt("data", "not-a-valid-fernet-key")

    def test_different_encryptions_differ(self, fernet_key: str):
        """같은 평문도 매번 다른 암호문을 생성한다 (Fernet은 nonce 포함)."""
        enc1 = AccountCrypto.encrypt("same", fernet_key)
        enc2 = AccountCrypto.encrypt("same", fernet_key)
        assert enc1 != enc2


# ---------------------------------------------------------------------------
# Account ORM Model
# ---------------------------------------------------------------------------


class TestAccountModel:
    """Account ORM 모델 구조 테스트."""

    def test_tablename(self):
        assert Account.__tablename__ == "accounts"

    def test_primary_key(self):
        pk_cols = [c.name for c in Account.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_columns_exist(self):
        col_names = {c.name for c in Account.__table__.columns}
        expected = {
            "id", "nickname",
            "kis_app_key_enc", "kis_app_secret_enc",
            "kis_account_no", "kis_account_prod",
            "kis_is_paper", "kis_hts_id",
            "strategy_type", "investment_prompt",
            "risk_overrides", "is_active",
            "created_at", "updated_at",
        }
        assert expected.issubset(col_names)

    def test_default_values(self):
        """ORM default는 Python-side default. 컬럼 정의에서 확인."""
        cols = Account.__table__.columns
        assert cols["is_active"].default.arg is True
        assert cols["kis_is_paper"].default.arg is True
        assert cols["kis_account_prod"].default.arg == "01"
        assert cols["strategy_type"].default.arg == "position"
        assert cols["investment_prompt"].default.arg == ""
        assert cols["nickname"].default.arg == ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestAccountConfig:
    """ACCOUNT_ENCRYPTION_KEY 설정 테스트."""

    def test_default_empty(self):
        s = make_settings(ACCOUNT_ENCRYPTION_KEY="")
        assert s.ACCOUNT_ENCRYPTION_KEY == ""

    def test_override(self):
        key = Fernet.generate_key().decode()
        s = make_settings(ACCOUNT_ENCRYPTION_KEY=key)
        assert s.ACCOUNT_ENCRYPTION_KEY == key


# ---------------------------------------------------------------------------
# Existing Models: account_id column
# ---------------------------------------------------------------------------


class TestExistingModelsAccountId:
    """기존 7개 모델에 account_id 컬럼 존재 확인."""

    @pytest.mark.parametrize(
        "model",
        [Order, PositionRecord, PortfolioSnapshot, ApprovalRequestDB, AgentMemory, DecisionLog],
    )
    def test_account_id_not_null(self, model):
        col = model.__table__.columns["account_id"]
        assert not col.nullable
        assert str(col.server_default.arg) == "default"  # type: ignore[union-attr]

    def test_job_executions_account_id_nullable(self):
        col = JobExecution.__table__.columns["account_id"]
        assert col.nullable

    @pytest.mark.parametrize(
        "model",
        [Order, PositionRecord, PortfolioSnapshot, ApprovalRequestDB, AgentMemory, DecisionLog, JobExecution],
    )
    def test_account_id_has_fk(self, model):
        col = model.__table__.columns["account_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "accounts.id" in fk_targets


# ---------------------------------------------------------------------------
# PortfolioSnapshot Unique Constraint
# ---------------------------------------------------------------------------


class TestPortfolioSnapshotConstraint:
    """PortfolioSnapshot unique constraint 변경 확인."""

    def test_unique_constraint_includes_account_id(self):
        constraints = PortfolioSnapshot.__table__.constraints
        unique_constraints = [
            c for c in constraints
            if hasattr(c, "columns") and c.__class__.__name__ == "UniqueConstraint"
        ]
        col_sets = [
            {col.name for col in uc.columns}
            for uc in unique_constraints
        ]
        assert {"account_id", "snapshot_date"} in col_sets

    def test_old_constraint_removed(self):
        constraints = PortfolioSnapshot.__table__.constraints
        unique_constraints = [
            c for c in constraints
            if hasattr(c, "columns") and c.__class__.__name__ == "UniqueConstraint"
        ]
        col_sets = [
            {col.name for col in uc.columns}
            for uc in unique_constraints
        ]
        # 기존 snapshot_date만의 unique는 없어야 함
        assert {"snapshot_date"} not in col_sets
