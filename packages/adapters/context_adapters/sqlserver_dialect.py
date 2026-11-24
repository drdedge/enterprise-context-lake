"""SQL Server dialect tuned for the lake's shared database (ADR-010).

Three things differ from SQLAlchemy's stock ``mssql+aioodbc`` / ``mssql+pyodbc`` dialects:

1. **Text is Unicode and compares like SQLite.** Every ``String``/``Text`` column is created as
   ``NVARCHAR(n|MAX) COLLATE Latin1_General_100_BIN2`` and every string parameter is sent as
   NVARCHAR. Unicode keeps non-Latin names intact; the binary collation makes ``=`` case-sensitive and
   ``ORDER BY`` code-point ordered, exactly as on SQLite, so both stores return the same answers; and
   NVARCHAR parameters against NVARCHAR columns keep index seeks (a VARCHAR/NVARCHAR mismatch forces
   a scan).
2. **Large IN lists become one JSON parameter.** SQL Server allows at most 2,100 parameters per
   statement. ``col IN (...)`` with many values is rendered as
   ``col IN (SELECT value FROM OPENJSON(?))`` with the list sent as a single JSON array (compatibility
   level 130 or later; the target database is at 150). One plan then serves every list size.
3. **Optional table-name prefix.** Where the account may not create a schema, the lake's tables live
   in ``dbo`` as ``lake_<name>``. The identifier preparer maps each logical table name to its
   physical name, so the store's queries are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.dialects import registry
from sqlalchemy.dialects.mssql.aioodbc import MSDialectAsync_aioodbc
from sqlalchemy.dialects.mssql.base import MSIdentifierPreparer, MSSQLCompiler, MSTypeCompiler
from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc
from sqlalchemy.sql import sqltypes

COLLATION = "Latin1_General_100_BIN2"
#: Lists at least this long go through OPENJSON instead of one parameter per value.
OPENJSON_IN_THRESHOLD = 50


class LakeTypeCompiler(MSTypeCompiler):
    def _nvarchar(self, type_: Any) -> str:
        length = getattr(type_, "length", None)
        # A NVARCHAR(n) key is limited to 4000 characters; anything longer is MAX
        size = "max" if not length or length > 4000 else str(length)
        return f"NVARCHAR({size}) COLLATE {COLLATION}"

    def visit_string(self, type_: Any, **kw: Any) -> str:
        return self._nvarchar(type_)

    def visit_text(self, type_: Any, **kw: Any) -> str:
        return f"NVARCHAR(max) COLLATE {COLLATION}"

    visit_VARCHAR = visit_string
    visit_NVARCHAR = visit_string
    visit_unicode = visit_string
    visit_unicode_text = visit_text
    visit_TEXT = visit_text


class LakeCompiler(MSSQLCompiler):
    def _literal_execute_expanding_parameter(self, name: str, parameter: Any, values: Any, *, taken_names: Any) -> Any:
        scalar = (
            not parameter.literal_execute
            and len(values) >= OPENJSON_IN_THRESHOLD
            and not parameter.type._unwrapped_dialect_impl(self.dialect)._is_tuple_type
        )
        if scalar:
            if all(isinstance(v, str) for v in values):
                cast = f"NVARCHAR(4000)) COLLATE {COLLATION}"
            elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                cast = "BIGINT)"
            else:
                cast = ""
            if cast:
                key = name + "_json"
                while key in taken_names:
                    key += "_"
                taken_names.add(key)
                placeholder = self.bindtemplate % {"name": key}
                return [(key, json.dumps(list(values)))], (
                    f"SELECT CAST([value] AS {cast} FROM OPENJSON({placeholder})"
                )
        return super()._literal_execute_expanding_parameter(name, parameter, values, taken_names=taken_names)


class LakePreparer(MSIdentifierPreparer):
    """Maps logical table names to physical ones (``entities`` -> ``lake_entities``) when prefixing."""

    lake_names: dict[str, str] = {}

    def quote(self, ident: str) -> str:  # type: ignore[override]
        return super().quote(self.lake_names.get(ident, ident))


class _LakeNString(sqltypes.String):
    """Every string parameter goes to the driver as NVARCHAR (SQL_WVARCHAR)."""

    def get_dbapi_type(self, dbapi: Any) -> Any:
        if self.length in (None, "max") or (isinstance(self.length, int) and self.length >= 2000):
            return (dbapi.SQL_WVARCHAR, 0, 0)
        return dbapi.SQL_WVARCHAR


_COLSPECS_UPDATE = {sqltypes.String: _LakeNString}


class LakeDialect_aioodbc(MSDialectAsync_aioodbc):
    statement_compiler = LakeCompiler
    type_compiler_cls = LakeTypeCompiler
    preparer = LakePreparer
    supports_statement_cache = True
    colspecs = {**MSDialectAsync_aioodbc.colspecs, **_COLSPECS_UPDATE}


class LakeDialect_pyodbc(MSDialect_pyodbc):
    statement_compiler = LakeCompiler
    type_compiler_cls = LakeTypeCompiler
    preparer = LakePreparer
    supports_statement_cache = True
    colspecs = {**MSDialect_pyodbc.colspecs, **_COLSPECS_UPDATE}


registry.register("mssql.lake_aioodbc", __name__, "LakeDialect_aioodbc")
registry.register("mssql.lake_pyodbc", __name__, "LakeDialect_pyodbc")


def configure_dialect(dialect: Any, *, table_names: dict[str, str]) -> None:
    """Give this engine's preparer its own logical-to-physical table map."""
    prep = dialect.identifier_preparer
    prep.lake_names = dict(table_names)
    # The preparer caches quoted strings; drop anything quoted before the map was set
    if hasattr(prep, "_strings"):
        prep._strings.clear()  # noqa: SLF001


__all__ = [
    "COLLATION",
    "OPENJSON_IN_THRESHOLD",
    "LakeDialect_aioodbc",
    "LakeDialect_pyodbc",
    "configure_dialect",
]
