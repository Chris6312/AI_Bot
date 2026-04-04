from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.exact_numeric import ExactNumeric


class CryptoPaperOrder(Base):
    __tablename__ = "crypto_paper_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    account_key = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    ohlcv_pair = Column(String(32), nullable=True)
    side = Column(String(8), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    requested_quantity = Column(ExactNumeric(36, 18), nullable=False, default=0)
    requested_price = Column(ExactNumeric(36, 18), nullable=True)
    filled_quantity = Column(ExactNumeric(36, 18), nullable=False, default=0)
    avg_fill_price = Column(ExactNumeric(36, 18), nullable=True)
    intent_id = Column(String(64), nullable=True, index=True)
    source = Column(String(64), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
