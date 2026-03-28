from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from app.core.database import Base

class Trade(Base):
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True, index=True)
    trade_id = Column(String(50), unique=True, nullable=False, index=True)
    account_id = Column(String(50), ForeignKey("accounts.account_id"))
    
    ticker = Column(String(10), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    strategy = Column(String(50), nullable=False, index=True)
    
    entry_time = Column(DateTime(timezone=True), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    shares = Column(Integer, nullable=False)
    entry_cost = Column(Float, nullable=False)
    
    exit_time = Column(DateTime(timezone=True))
    exit_price = Column(Float)
    exit_proceeds = Column(Float)
    exit_trigger = Column(String(50))
    
    gross_pnl = Column(Float)
    net_pnl = Column(Float)
    return_pct = Column(Float)
    duration_minutes = Column(Integer)
    
    entry_reasoning = Column(JSON)
    exit_reasoning = Column(JSON)
    
    execution_id = Column(String(50))
    entry_order_id = Column(String(50))
    exit_order_id = Column(String(50))
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
