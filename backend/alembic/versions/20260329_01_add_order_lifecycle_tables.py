"""add order lifecycle tables

Revision ID: 20260329_01
Revises: 
Create Date: 2026-03-29 13:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '20260329_01'
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_intents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('intent_id', sa.String(length=64), nullable=False),
        sa.Column('account_id', sa.String(length=50), nullable=False),
        sa.Column('asset_class', sa.String(length=20), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('requested_quantity', sa.Float(), nullable=False),
        sa.Column('requested_price', sa.Float(), nullable=True),
        sa.Column('filled_quantity', sa.Float(), nullable=False, server_default='0'),
        sa.Column('avg_fill_price', sa.Float(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('execution_source', sa.String(length=50), nullable=False),
        sa.Column('submitted_order_id', sa.String(length=64), nullable=True),
        sa.Column('position_id', sa.Integer(), nullable=True),
        sa.Column('trade_id', sa.Integer(), nullable=True),
        sa.Column('rejection_reason', sa.String(length=255), nullable=True),
        sa.Column('context_json', sa.JSON(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_fill_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_fill_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index('ix_order_intents_id', 'order_intents', ['id'])
    op.create_index('ix_order_intents_intent_id', 'order_intents', ['intent_id'], unique=True)
    op.create_index('ix_order_intents_account_id', 'order_intents', ['account_id'])
    op.create_index('ix_order_intents_asset_class', 'order_intents', ['asset_class'])
    op.create_index('ix_order_intents_symbol', 'order_intents', ['symbol'])
    op.create_index('ix_order_intents_status', 'order_intents', ['status'])
    op.create_index('ix_order_intents_execution_source', 'order_intents', ['execution_source'])
    op.create_index('ix_order_intents_submitted_order_id', 'order_intents', ['submitted_order_id'])

    op.create_table(
        'order_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('intent_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('message', sa.String(length=255), nullable=True),
        sa.Column('payload_json', sa.JSON(), nullable=True),
        sa.Column('event_time', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['intent_id'], ['order_intents.intent_id']),
    )
    op.create_index('ix_order_events_id', 'order_events', ['id'])
    op.create_index('ix_order_events_intent_id', 'order_events', ['intent_id'])
    op.create_index('ix_order_events_event_type', 'order_events', ['event_type'])
    op.create_index('ix_order_events_status', 'order_events', ['status'])
    op.create_index('ix_order_events_event_time', 'order_events', ['event_time'])


def downgrade() -> None:
    op.drop_index('ix_order_events_event_time', table_name='order_events')
    op.drop_index('ix_order_events_status', table_name='order_events')
    op.drop_index('ix_order_events_event_type', table_name='order_events')
    op.drop_index('ix_order_events_intent_id', table_name='order_events')
    op.drop_index('ix_order_events_id', table_name='order_events')
    op.drop_table('order_events')

    op.drop_index('ix_order_intents_submitted_order_id', table_name='order_intents')
    op.drop_index('ix_order_intents_execution_source', table_name='order_intents')
    op.drop_index('ix_order_intents_status', table_name='order_intents')
    op.drop_index('ix_order_intents_symbol', table_name='order_intents')
    op.drop_index('ix_order_intents_asset_class', table_name='order_intents')
    op.drop_index('ix_order_intents_account_id', table_name='order_intents')
    op.drop_index('ix_order_intents_intent_id', table_name='order_intents')
    op.drop_index('ix_order_intents_id', table_name='order_intents')
    op.drop_table('order_intents')
