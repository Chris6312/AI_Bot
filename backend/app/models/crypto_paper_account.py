from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.exact_numeric import ExactNumeric


class CryptoPaperAccount(Base):
    __tablename__ = "crypto_paper_accounts"

    id = Column(Integer, primary_key=True, index=True)
    account_key = Column(String(64), unique=True, nullable=False, index=True)
    base_currency = Column(String(16), nullable=False, default="USD")
    cash_balance = Column(ExactNumeric(36, 18), nullable=False, default=0)
    starting_balance = Column(ExactNumeric(36, 18), nullable=False, default=0)
    realized_pnl = Column(ExactNumeric(36, 18), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())