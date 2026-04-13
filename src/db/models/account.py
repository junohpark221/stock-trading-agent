"""Phase 8 account ORM model and encryption utility."""

from cryptography.fernet import Fernet
from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class Account(TimestampMixin, Base):
    """투자 계정 — 멀티 계정 지원을 위한 계정 정보 테이블.

    KIS API 인증 정보는 Fernet으로 암호화하여 저장한다.
    id="default"는 레거시 단일 계정 호환용.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        Index("ix_accounts_is_active", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    kis_app_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kis_app_secret_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kis_account_no: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    kis_account_prod: Mapped[str] = mapped_column(String(5), nullable=False, default="01")
    kis_is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    kis_hts_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False, default="position")
    investment_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    risk_tolerance: Mapped[str] = mapped_column(
        String(20), nullable=False, default="moderate",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AccountCrypto:
    """Fernet symmetric encryption for account credentials.

    Usage::

        key = settings.ACCOUNT_ENCRYPTION_KEY
        encrypted = AccountCrypto.encrypt("my_secret", key)
        plaintext = AccountCrypto.decrypt(encrypted, key)
    """

    @staticmethod
    def encrypt(plaintext: str, key: str) -> str:
        """Encrypt plaintext using Fernet. Returns base64-encoded ciphertext."""
        f = Fernet(key.encode())
        return f.encrypt(plaintext.encode()).decode()

    @staticmethod
    def decrypt(ciphertext: str, key: str) -> str:
        """Decrypt Fernet ciphertext. Returns original plaintext."""
        f = Fernet(key.encode())
        return f.decrypt(ciphertext.encode()).decode()
