from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator


class ExactNumeric(TypeDecorator):
    """Preserve Decimal precision in SQLite while using NUMERIC elsewhere."""

    cache_ok = True
    impl = Numeric

    def __init__(self, precision: int = 36, scale: int = 18):
        super().__init__()
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect):
        if dialect.name == 'sqlite':
            return dialect.type_descriptor(String(128))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        dec = Decimal(str(value))
        text = format(dec, 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.') or '0'
        if dialect.name == 'sqlite':
            return text
        return dec

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value))
