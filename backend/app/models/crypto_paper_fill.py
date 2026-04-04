from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.exact_numeric import ExactNumeric


class CryptoPaperFill(Base):
    __tablename__ = "crypto_paper_fills"

    id = Column(Integer, primary_key=True, index=True)
    fill_id = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(String(64), nullable=False, index=True)
    account_key = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    ohlcv_pair = Column(String(32), nullable=True)
    side = Column(String(8), nullable=False)
    quantity = Column(ExactNumeric(36, 18), nullable=False, default=0)
    price = Column(ExactNumeric(36, 18), nullable=False, default=0)
    notional = Column(ExactNumeric(36, 18), nullable=False, default=0)
    fee = Column(ExactNumeric(36, 18), nullable=False, default=0)
    filled_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
