from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, select
from sqlalchemy.orm import column_property
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.order_intent import OrderIntent


class OrderEvent(Base):
    __tablename__ = "order_events"

    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(String(64), ForeignKey("order_intents.intent_id"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)
    status = Column(String(32), nullable=False, index=True)
    message = Column(String(255))
    payload_json = Column(JSON)
    event_time = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    order_intent_id = column_property(
        select(OrderIntent.id)
        .where(OrderIntent.intent_id == intent_id)
        .correlate_except(OrderIntent)
        .scalar_subquery()
    )
