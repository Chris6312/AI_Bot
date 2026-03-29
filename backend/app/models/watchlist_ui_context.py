from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func

from app.core.database import Base


class WatchlistUiContext(Base):
    __tablename__ = 'watchlist_ui_context'

    id = Column(Integer, primary_key=True, index=True)
    upload_id = Column(String(64), ForeignKey('watchlist_uploads.upload_id'), nullable=False, index=True)
    summary_json = Column(JSON, nullable=False)
    provider_limitations_json = Column(JSON, nullable=False)
    symbol_context_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
