"""add crypto paper ledger tables

Revision ID: 20260404_01
Revises: 20260329_03
Create Date: 2026-04-04 18:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_01"
down_revision = "20260329_03"
branch_labels = None
depends_on = None


NUMERIC = sa.Numeric(36, 18)


def upgrade() -> None:

    op.create_table(
        "crypto_paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("base_currency", sa.String(length=16), nullable=False, server_default="USD"),
        sa.Column("cash_balance", NUMERIC, nullable=False, server_default="0"),
        sa.Column("starting_balance", NUMERIC, nullable=False, server_default="0"),
        sa.Column("realized_pnl", NUMERIC, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_crypto_paper_accounts_account_key",
        "crypto_paper_accounts",
        ["account_key"],
        unique=True,
    )


    op.create_table(
        "crypto_paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("ohlcv_pair", sa.String(length=32)),
        sa.Column("quantity", NUMERIC, nullable=False, server_default="0"),
        sa.Column("avg_price", NUMERIC, nullable=False, server_default="0"),
        sa.Column("total_cost", NUMERIC, nullable=False, server_default="0"),
        sa.Column("realized_pnl", NUMERIC, nullable=False, server_default="0"),
        sa.Column("entry_time_utc", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_crypto_paper_positions_account_key",
        "crypto_paper_positions",
        ["account_key"],
    )

    op.create_index(
        "ix_crypto_paper_positions_symbol",
        "crypto_paper_positions",
        ["symbol"],
    )

    op.create_index(
        "ix_crypto_paper_positions_is_open",
        "crypto_paper_positions",
        ["is_open"],
    )


    op.create_table(
        "crypto_paper_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("ohlcv_pair", sa.String(length=32)),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_quantity", NUMERIC, nullable=False),
        sa.Column("requested_price", NUMERIC),
        sa.Column("filled_quantity", NUMERIC, nullable=False, server_default="0"),
        sa.Column("avg_fill_price", NUMERIC),
        sa.Column("intent_id", sa.String(length=64)),
        sa.Column("source", sa.String(length=64)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_crypto_paper_orders_order_id",
        "crypto_paper_orders",
        ["order_id"],
        unique=True,
    )

    op.create_index(
        "ix_crypto_paper_orders_symbol",
        "crypto_paper_orders",
        ["symbol"],
    )

    op.create_index(
        "ix_crypto_paper_orders_status",
        "crypto_paper_orders",
        ["status"],
    )


    op.create_table(
        "crypto_paper_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fill_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("ohlcv_pair", sa.String(length=32)),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", NUMERIC, nullable=False),
        sa.Column("price", NUMERIC, nullable=False),
        sa.Column("notional", NUMERIC, nullable=False),
        sa.Column("fee", NUMERIC, nullable=False, server_default="0"),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_crypto_paper_fills_fill_id",
        "crypto_paper_fills",
        ["fill_id"],
        unique=True,
    )

    op.create_index(
        "ix_crypto_paper_fills_symbol",
        "crypto_paper_fills",
        ["symbol"],
    )

    op.create_index(
        "ix_crypto_paper_fills_side",
        "crypto_paper_fills",
        ["side"],
    )

    op.create_index(
        "ix_crypto_paper_fills_filled_at",
        "crypto_paper_fills",
        ["filled_at"],
    )


def downgrade() -> None:

    op.drop_index("ix_crypto_paper_fills_filled_at", table_name="crypto_paper_fills")
    op.drop_index("ix_crypto_paper_fills_side", table_name="crypto_paper_fills")
    op.drop_index("ix_crypto_paper_fills_symbol", table_name="crypto_paper_fills")
    op.drop_index("ix_crypto_paper_fills_fill_id", table_name="crypto_paper_fills")
    op.drop_table("crypto_paper_fills")


    op.drop_index("ix_crypto_paper_orders_status", table_name="crypto_paper_orders")
    op.drop_index("ix_crypto_paper_orders_symbol", table_name="crypto_paper_orders")
    op.drop_index("ix_crypto_paper_orders_order_id", table_name="crypto_paper_orders")
    op.drop_table("crypto_paper_orders")


    op.drop_index("ix_crypto_paper_positions_is_open", table_name="crypto_paper_positions")
    op.drop_index("ix_crypto_paper_positions_symbol", table_name="crypto_paper_positions")
    op.drop_index("ix_crypto_paper_positions_account_key", table_name="crypto_paper_positions")
    op.drop_table("crypto_paper_positions")


    op.drop_index("ix_crypto_paper_accounts_account_key", table_name="crypto_paper_accounts")
    op.drop_table("crypto_paper_accounts")