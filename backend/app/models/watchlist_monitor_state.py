from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func

from app.core.database import Base


PENDING_EVALUATION = 'PENDING_EVALUATION'
MONITOR_ONLY = 'MONITOR_ONLY'


class WatchlistMonitorState(Base):
    __tablename__ = 'watchlist_monitor_state'

    id = Column(Integer, primary_key=True, index=True)
    watchlist_symbol_id = Column(Integer, ForeignKey('watchlist_symbols.id'), nullable=False, unique=True, index=True)
    upload_id = Column(String(64), nullable=False, index=True)
    scope = Column(String(32), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    monitoring_status = Column(String(32), nullable=False, default='ACTIVE', index=True)
    latest_decision_state = Column(String(64), nullable=False, default='PENDING_EVALUATION', index=True)
    latest_decision_reason = Column(String(255), nullable=True)
    decision_context_json = Column(JSON, nullable=False)
    required_timeframes_json = Column(JSON, nullable=False)
    evaluation_interval_seconds = Column(Integer, nullable=True)
    last_decision_at_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    last_evaluated_at_utc = Column(DateTime(timezone=True), nullable=True)
    next_evaluation_at_utc = Column(DateTime(timezone=True), nullable=True, index=True)
    last_market_data_at_utc = Column(DateTime(timezone=True), nullable=True)

    # Monotonic runner-protection milestones — survive process restarts and watchlist upload cycles
    protection_mode_high_water = Column(String(32), nullable=True)
    tp_touched_at_utc = Column(DateTime(timezone=True), nullable=True)
    break_even_promoted_at_utc = Column(DateTime(timezone=True), nullable=True)
    stronger_margin_promoted_at_utc = Column(DateTime(timezone=True), nullable=True)
    promoted_protective_floor = Column(Float, nullable=True)
    highest_protective_floor = Column(Float, nullable=True)
    peak_price_since_entry = Column(Float, nullable=True)
    impulse_trail_armed_at_utc = Column(DateTime(timezone=True), nullable=True)
    impulse_trailing_stop = Column(Float, nullable=True)
    scale_out_taken = Column(Boolean, nullable=True)
    last_management_evaluated_at_utc = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
