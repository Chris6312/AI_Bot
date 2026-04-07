from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, JSON
from sqlalchemy.sql import func
from app.core.database import Base

class Position(Base):
    __tablename__ = "positions"
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(String(50), ForeignKey("accounts.account_id"))
    
    ticker = Column(String(10), nullable=False, index=True)
    shares = Column(Integer, nullable=False)
    avg_entry_price = Column(Float, nullable=False)
    current_price = Column(Float)
    
    unrealized_pnl = Column(Float)
    unrealized_pnl_pct = Column(Float)
    
    strategy = Column(String(50), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    entry_reasoning = Column(JSON)
    
    stop_loss = Column(Float, nullable=True)
    profit_target = Column(Float, nullable=True)
    peak_price = Column(Float, nullable=True)
    trailing_stop = Column(Float, nullable=True)
    
    is_open = Column(Boolean, default=True, index=True)
    execution_id = Column(String(50))

    # Frozen management policy — set once at confirmed fill, never overwritten by subsequent watchlist uploads
    frozen_exit_template = Column(String(64), nullable=True)
    frozen_max_hold_hours = Column(Integer, nullable=True)
    frozen_management_policy_version = Column(String(32), nullable=True)
    entry_watchlist_upload_id = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
