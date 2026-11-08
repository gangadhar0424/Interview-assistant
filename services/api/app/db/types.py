"""Minimal pgvector column type (DDL/metadata only).

Values are written and queried with explicit SQL (`CAST(CAST(:v AS text) AS vector)`), which keeps
asyncpg's parameter handling trivial and avoids a numpy dependency.
"""

from typing import Any

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[Any]):
    cache_ok = True

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dimension})"


def vector_literal(values: list[float]) -> str:
    """Text form pgvector parses: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
