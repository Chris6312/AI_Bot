"""add position management freeze fields and milestone columns

Revision ID: 20260501_01
Revises: 20260404_01
Create Date: 2026-05-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '20260501_01'
down_revision = '20260404_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen management policy on open stock positions (set once at fill time, never overwritten)
    op.add_column('positions', sa.Column('frozen_exit_template', sa.String(length=64), nullable=True))
    op.add_column('positions', sa.Column('frozen_max_hold_hours', sa.Integer(), nullable=True))
    op.add_column('positions', sa.Column('frozen_management_policy_version', sa.String(length=32), nullable=True))
    op.add_column('positions', sa.Column('entry_watchlist_upload_id', sa.String(length=64), nullable=True))

    # Monotonic runner-protection milestones on watchlist monitor state
    # (survive process restarts and watchlist upload cycles)
    op.add_column('watchlist_monitor_state', sa.Column('protection_mode_high_water', sa.String(length=32), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('tp_touched_at_utc', sa.DateTime(timezone=True), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('break_even_promoted_at_utc', sa.DateTime(timezone=True), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('stronger_margin_promoted_at_utc', sa.DateTime(timezone=True), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('promoted_protective_floor', sa.Float(), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('highest_protective_floor', sa.Float(), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('peak_price_since_entry', sa.Float(), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('impulse_trail_armed_at_utc', sa.DateTime(timezone=True), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('impulse_trailing_stop', sa.Float(), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('scale_out_taken', sa.Boolean(), nullable=True))
    op.add_column('watchlist_monitor_state', sa.Column('last_management_evaluated_at_utc', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('watchlist_monitor_state', 'last_management_evaluated_at_utc')
    op.drop_column('watchlist_monitor_state', 'scale_out_taken')
    op.drop_column('watchlist_monitor_state', 'impulse_trailing_stop')
    op.drop_column('watchlist_monitor_state', 'impulse_trail_armed_at_utc')
    op.drop_column('watchlist_monitor_state', 'peak_price_since_entry')
    op.drop_column('watchlist_monitor_state', 'highest_protective_floor')
    op.drop_column('watchlist_monitor_state', 'promoted_protective_floor')
    op.drop_column('watchlist_monitor_state', 'stronger_margin_promoted_at_utc')
    op.drop_column('watchlist_monitor_state', 'break_even_promoted_at_utc')
    op.drop_column('watchlist_monitor_state', 'tp_touched_at_utc')
    op.drop_column('watchlist_monitor_state', 'protection_mode_high_water')

    op.drop_column('positions', 'entry_watchlist_upload_id')
    op.drop_column('positions', 'frozen_management_policy_version')
    op.drop_column('positions', 'frozen_max_hold_hours')
    op.drop_column('positions', 'frozen_exit_template')
