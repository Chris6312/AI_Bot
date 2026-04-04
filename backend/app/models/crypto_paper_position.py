from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.exact_numeric import ExactNumeric


class CryptoPaperPosition(Base):
    __tablename__ = "crypto_paper_positions"

    id = Column(Integer, primary_key=True, index=True)
    account_key = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    ohlcv_pair = Column(String(32), nullable=True)
    quantity = Column(ExactNumeric(36, 18), nullable=False, default=0)
    avg_price = Column(ExactNumeric(36, 18), nullable=False, default=0)
    total_cost = Column(ExactNumeric(36, 18), nullable=False, default=0)
    realized_pnl = Column(ExactNumeric(36, 18), nullable=False, default=0)
    entry_time_utc = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    is_open = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
