from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String
from sqlalchemy.sql import func

from app.core.database import Base


class WatchlistUpload(Base):
    __tablename__ = 'watchlist_uploads'

    id = Column(Integer, primary_key=True, index=True)
    upload_id = Column(String(64), unique=True, nullable=False, index=True)
    scan_id = Column(String(64), unique=True, nullable=False, index=True)
    schema_version = Column(String(64), nullable=False, index=True)
    provider = Column(String(100), nullable=False, index=True)
    scope = Column(String(32), nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)
    source_user_id = Column(String(64), index=True)
    source_channel_id = Column(String(64), index=True)
    source_message_id = Column(String(64), index=True)
    payload_hash = Column(String(64), nullable=False, index=True)
    generated_at_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    watchlist_expires_at_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    validation_status = Column(String(32), nullable=False, index=True)
    rejection_reason = Column(String(255))
    market_regime = Column(String(32))
    selected_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=False, index=True)
    validation_result_json = Column(JSON)
    raw_payload_json = Column(JSON, nullable=False)
    bot_payload_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
