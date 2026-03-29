from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func

from app.core.database import Base


class WatchlistSymbol(Base):
    __tablename__ = 'watchlist_symbols'

    id = Column(Integer, primary_key=True, index=True)
    upload_id = Column(String(64), ForeignKey('watchlist_uploads.upload_id'), nullable=False, index=True)
    scope = Column(String(32), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    quote_currency = Column(String(16), nullable=False)
    asset_class = Column(String(20), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    trade_direction = Column(String(16), nullable=False)
    priority_rank = Column(Integer, nullable=False, index=True)
    tier = Column(String(16), nullable=False)
    bias = Column(String(16), nullable=False)
    setup_template = Column(String(64), nullable=False)
    bot_timeframes = Column(JSON, nullable=False)
    exit_template = Column(String(64), nullable=False)
    max_hold_hours = Column(Integer, nullable=False)
    risk_flags = Column(JSON, nullable=False)
    monitoring_status = Column(String(32), nullable=False, default='ACTIVE', index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
