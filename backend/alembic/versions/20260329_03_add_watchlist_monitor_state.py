"""add watchlist monitor state table

Revision ID: 20260329_03
Revises: 20260329_02
Create Date: 2026-03-29 15:55:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260329_03'
down_revision = '20260329_02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'watchlist_monitor_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('watchlist_symbol_id', sa.Integer(), nullable=False),
        sa.Column('upload_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('monitoring_status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('latest_decision_state', sa.String(length=64), nullable=False, server_default='PENDING_EVALUATION'),
        sa.Column('latest_decision_reason', sa.String(length=255), nullable=True),
        sa.Column('decision_context_json', sa.JSON(), nullable=False),
        sa.Column('required_timeframes_json', sa.JSON(), nullable=False),
        sa.Column('evaluation_interval_seconds', sa.Integer(), nullable=True),
        sa.Column('last_decision_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_evaluated_at_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_evaluation_at_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_market_data_at_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['watchlist_symbol_id'], ['watchlist_symbols.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('watchlist_symbol_id'),
    )
    op.create_index(op.f('ix_watchlist_monitor_state_id'), 'watchlist_monitor_state', ['id'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_watchlist_symbol_id'), 'watchlist_monitor_state', ['watchlist_symbol_id'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_upload_id'), 'watchlist_monitor_state', ['upload_id'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_scope'), 'watchlist_monitor_state', ['scope'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_symbol'), 'watchlist_monitor_state', ['symbol'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_monitoring_status'), 'watchlist_monitor_state', ['monitoring_status'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_latest_decision_state'), 'watchlist_monitor_state', ['latest_decision_state'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_last_decision_at_utc'), 'watchlist_monitor_state', ['last_decision_at_utc'], unique=False)
    op.create_index(op.f('ix_watchlist_monitor_state_next_evaluation_at_utc'), 'watchlist_monitor_state', ['next_evaluation_at_utc'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_watchlist_monitor_state_next_evaluation_at_utc'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_last_decision_at_utc'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_latest_decision_state'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_monitoring_status'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_symbol'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_scope'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_upload_id'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_watchlist_symbol_id'), table_name='watchlist_monitor_state')
    op.drop_index(op.f('ix_watchlist_monitor_state_id'), table_name='watchlist_monitor_state')
    op.drop_table('watchlist_monitor_state')
