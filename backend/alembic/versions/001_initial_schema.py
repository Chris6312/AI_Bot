"""initial schema

Revision ID: 001
Revises: 
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Create accounts table
    op.create_table('accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.String(length=50), nullable=False),
        sa.Column('account_type', sa.String(length=20), nullable=False),
        sa.Column('cash', sa.Float(), nullable=False),
        sa.Column('equity', sa.Float(), nullable=False),
        sa.Column('buying_power', sa.Float(), nullable=True),
        sa.Column('realized_pnl', sa.Float(), nullable=True),
        sa.Column('unrealized_pnl', sa.Float(), nullable=True),
        sa.Column('daily_pnl', sa.Float(), nullable=True),
        sa.Column('trades_today', sa.Integer(), nullable=True),
        sa.Column('open_positions_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_accounts_account_id'), 'accounts', ['account_id'], unique=True)
    op.create_index(op.f('ix_accounts_id'), 'accounts', ['id'], unique=False)

    # Create positions table
    op.create_table('positions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.String(length=50), nullable=True),
        sa.Column('ticker', sa.String(length=10), nullable=False),
        sa.Column('shares', sa.Integer(), nullable=False),
        sa.Column('avg_entry_price', sa.Float(), nullable=False),
        sa.Column('current_price', sa.Float(), nullable=True),
        sa.Column('unrealized_pnl', sa.Float(), nullable=True),
        sa.Column('unrealized_pnl_pct', sa.Float(), nullable=True),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('entry_reasoning', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('stop_loss', sa.Float(), nullable=False),
        sa.Column('profit_target', sa.Float(), nullable=False),
        sa.Column('peak_price', sa.Float(), nullable=False),
        sa.Column('trailing_stop', sa.Float(), nullable=True),
        sa.Column('is_open', sa.Boolean(), nullable=True),
        sa.Column('execution_id', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.account_id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_positions_id'), 'positions', ['id'], unique=False)
    op.create_index(op.f('ix_positions_is_open'), 'positions', ['is_open'], unique=False)
    op.create_index(op.f('ix_positions_ticker'), 'positions', ['ticker'], unique=False)

    # Create trades table
    op.create_table('trades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('trade_id', sa.String(length=50), nullable=False),
        sa.Column('account_id', sa.String(length=50), nullable=True),
        sa.Column('ticker', sa.String(length=10), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('shares', sa.Integer(), nullable=False),
        sa.Column('entry_cost', sa.Float(), nullable=False),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('exit_proceeds', sa.Float(), nullable=True),
        sa.Column('exit_trigger', sa.String(length=50), nullable=True),
        sa.Column('gross_pnl', sa.Float(), nullable=True),
        sa.Column('net_pnl', sa.Float(), nullable=True),
        sa.Column('return_pct', sa.Float(), nullable=True),
        sa.Column('duration_minutes', sa.Integer(), nullable=True),
        sa.Column('entry_reasoning', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('exit_reasoning', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('execution_id', sa.String(length=50), nullable=True),
        sa.Column('entry_order_id', sa.String(length=50), nullable=True),
        sa.Column('exit_order_id', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.account_id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trades_entry_time'), 'trades', ['entry_time'], unique=False)
    op.create_index(op.f('ix_trades_id'), 'trades', ['id'], unique=False)
    op.create_index(op.f('ix_trades_strategy'), 'trades', ['strategy'], unique=False)
    op.create_index(op.f('ix_trades_ticker'), 'trades', ['ticker'], unique=False)
    op.create_index(op.f('ix_trades_trade_id'), 'trades', ['trade_id'], unique=True)

def downgrade():
    op.drop_table('trades')
    op.drop_table('positions')
    op.drop_table('accounts')
