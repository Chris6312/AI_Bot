"""add watchlist intake tables

Revision ID: 20260329_02
Revises: 20260329_01
Create Date: 2026-03-29 15:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260329_02'
down_revision = '20260329_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'watchlist_uploads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('upload_id', sa.String(length=64), nullable=False),
        sa.Column('scan_id', sa.String(length=64), nullable=False),
        sa.Column('schema_version', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=100), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('source_user_id', sa.String(length=64), nullable=True),
        sa.Column('source_channel_id', sa.String(length=64), nullable=True),
        sa.Column('source_message_id', sa.String(length=64), nullable=True),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('generated_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('watchlist_expires_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('validation_status', sa.String(length=32), nullable=False),
        sa.Column('rejection_reason', sa.String(length=255), nullable=True),
        sa.Column('market_regime', sa.String(length=32), nullable=True),
        sa.Column('selected_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('validation_result_json', sa.JSON(), nullable=True),
        sa.Column('raw_payload_json', sa.JSON(), nullable=False),
        sa.Column('bot_payload_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('upload_id'),
        sa.UniqueConstraint('scan_id'),
    )
    op.create_index(op.f('ix_watchlist_uploads_id'), 'watchlist_uploads', ['id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_upload_id'), 'watchlist_uploads', ['upload_id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_scan_id'), 'watchlist_uploads', ['scan_id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_schema_version'), 'watchlist_uploads', ['schema_version'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_provider'), 'watchlist_uploads', ['provider'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_scope'), 'watchlist_uploads', ['scope'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_source'), 'watchlist_uploads', ['source'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_source_user_id'), 'watchlist_uploads', ['source_user_id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_source_channel_id'), 'watchlist_uploads', ['source_channel_id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_source_message_id'), 'watchlist_uploads', ['source_message_id'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_payload_hash'), 'watchlist_uploads', ['payload_hash'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_generated_at_utc'), 'watchlist_uploads', ['generated_at_utc'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_received_at_utc'), 'watchlist_uploads', ['received_at_utc'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_watchlist_expires_at_utc'), 'watchlist_uploads', ['watchlist_expires_at_utc'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_validation_status'), 'watchlist_uploads', ['validation_status'], unique=False)
    op.create_index(op.f('ix_watchlist_uploads_is_active'), 'watchlist_uploads', ['is_active'], unique=False)

    op.create_table(
        'watchlist_symbols',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('upload_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('quote_currency', sa.String(length=16), nullable=False),
        sa.Column('asset_class', sa.String(length=20), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('trade_direction', sa.String(length=16), nullable=False),
        sa.Column('priority_rank', sa.Integer(), nullable=False),
        sa.Column('tier', sa.String(length=16), nullable=False),
        sa.Column('bias', sa.String(length=16), nullable=False),
        sa.Column('setup_template', sa.String(length=64), nullable=False),
        sa.Column('bot_timeframes', sa.JSON(), nullable=False),
        sa.Column('exit_template', sa.String(length=64), nullable=False),
        sa.Column('max_hold_hours', sa.Integer(), nullable=False),
        sa.Column('risk_flags', sa.JSON(), nullable=False),
        sa.Column('monitoring_status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['upload_id'], ['watchlist_uploads.upload_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_watchlist_symbols_id'), 'watchlist_symbols', ['id'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_upload_id'), 'watchlist_symbols', ['upload_id'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_scope'), 'watchlist_symbols', ['scope'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_symbol'), 'watchlist_symbols', ['symbol'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_asset_class'), 'watchlist_symbols', ['asset_class'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_priority_rank'), 'watchlist_symbols', ['priority_rank'], unique=False)
    op.create_index(op.f('ix_watchlist_symbols_monitoring_status'), 'watchlist_symbols', ['monitoring_status'], unique=False)

    op.create_table(
        'watchlist_ui_context',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('upload_id', sa.String(length=64), nullable=False),
        sa.Column('summary_json', sa.JSON(), nullable=False),
        sa.Column('provider_limitations_json', sa.JSON(), nullable=False),
        sa.Column('symbol_context_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['upload_id'], ['watchlist_uploads.upload_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_watchlist_ui_context_id'), 'watchlist_ui_context', ['id'], unique=False)
    op.create_index(op.f('ix_watchlist_ui_context_upload_id'), 'watchlist_ui_context', ['upload_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_watchlist_ui_context_upload_id'), table_name='watchlist_ui_context')
    op.drop_index(op.f('ix_watchlist_ui_context_id'), table_name='watchlist_ui_context')
    op.drop_table('watchlist_ui_context')

    op.drop_index(op.f('ix_watchlist_symbols_monitoring_status'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_priority_rank'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_asset_class'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_symbol'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_scope'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_upload_id'), table_name='watchlist_symbols')
    op.drop_index(op.f('ix_watchlist_symbols_id'), table_name='watchlist_symbols')
    op.drop_table('watchlist_symbols')

    op.drop_index(op.f('ix_watchlist_uploads_is_active'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_validation_status'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_watchlist_expires_at_utc'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_received_at_utc'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_generated_at_utc'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_payload_hash'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_source_message_id'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_source_channel_id'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_source_user_id'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_source'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_scope'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_provider'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_schema_version'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_scan_id'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_upload_id'), table_name='watchlist_uploads')
    op.drop_index(op.f('ix_watchlist_uploads_id'), table_name='watchlist_uploads')
    op.drop_table('watchlist_uploads')
