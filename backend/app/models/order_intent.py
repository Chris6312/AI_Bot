from sqlalchemy import Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.sql import func

from app.core.database import Base


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(String(64), unique=True, nullable=False, index=True)
    account_id = Column(String(50), nullable=False, index=True)
    asset_class = Column(String(20), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    requested_quantity = Column(Float, nullable=False)
    requested_price = Column(Float)
    filled_quantity = Column(Float, nullable=False, default=0.0)
    avg_fill_price = Column(Float)
    status = Column(String(32), nullable=False, index=True)
    execution_source = Column(String(50), nullable=False, index=True)
    submitted_order_id = Column(String(64), index=True)
    position_id = Column(Integer)
    trade_id = Column(Integer)
    rejection_reason = Column(String(255))
    context_json = Column(JSON)
    submitted_at = Column(DateTime(timezone=True))
    first_fill_at = Column(DateTime(timezone=True))
    last_fill_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
