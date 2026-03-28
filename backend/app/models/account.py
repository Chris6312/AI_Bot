from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.sql import func
from app.core.database import Base

class Account(Base):
    __tablename__ = "accounts"
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(String(50), unique=True, nullable=False, index=True)
    account_type = Column(String(20), nullable=False)
    
    cash = Column(Float, nullable=False)
    equity = Column(Float, nullable=False)
    buying_power = Column(Float)
    
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    daily_pnl = Column(Float, default=0.0)
    
    trades_today = Column(Integer, default=0)
    open_positions_count = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
