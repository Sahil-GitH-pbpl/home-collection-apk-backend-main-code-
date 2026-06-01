from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class SyncPage:
    items: list[dict[str, Any]]
    next_cursor: str | None
    max_updated_at: datetime | None


class SyncRepository:
    ACTIVE_FILTER_TABLES = {"address", "panelrates"}
    _CURSOR_KEY_ALIAS = "__sync_cursor_key"
    _ADDRESS_CURSOR_COLUMNS = (
        "CenterID",
        "Atype",
        "code",
        "ABARID",
        "pname",
        "desi",
        "orgname",
        "address",
        "address1",
        "address2",
        "city",
        "pin",
        "area",
        "Omobile",
        "category",
        "Aprint",
        "title",
        "email",
        "BillingChargeMode",
    )

    def __init__(self, db: Session, database_name: str) -> None:
        self.db = db
        self.database_name = database_name

    def _get_primary_key_column(self, table_name: str) -> str | None:
        row = self.db.execute(
            text(
                """
                SELECT k.COLUMN_NAME
                FROM information_schema.TABLE_CONSTRAINTS t
                JOIN information_schema.KEY_COLUMN_USAGE k
                  ON t.CONSTRAINT_NAME = k.CONSTRAINT_NAME
                 AND t.TABLE_SCHEMA = k.TABLE_SCHEMA
                 AND t.TABLE_NAME = k.TABLE_NAME
                WHERE t.TABLE_SCHEMA = :schema_name
                  AND t.TABLE_NAME = :table_name
                  AND t.CONSTRAINT_TYPE = 'PRIMARY KEY'
                ORDER BY k.ORDINAL_POSITION
                LIMIT 1
                """
            ),
            {"schema_name": self.database_name, "table_name": table_name},
        ).mappings().first()
        if not row:
            return None
        return str(row["COLUMN_NAME"])

    def _get_fallback_order_column(self, table_name: str) -> str | None:
        row = self.db.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME <> 'updated_at'
                ORDER BY ORDINAL_POSITION
                LIMIT 1
                """
            ),
            {"schema_name": self.database_name, "table_name": table_name},
        ).mappings().first()
        if not row:
            return None
        return str(row["COLUMN_NAME"])

    def _get_active_flag_column(self, table_name: str) -> str | None:
        if table_name not in self.ACTIVE_FILTER_TABLES:
            return None
        row = self.db.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND LOWER(COLUMN_NAME) = 'active'
                LIMIT 1
                """
            ),
            {"schema_name": self.database_name, "table_name": table_name},
        ).mappings().first()
        if not row:
            return None
        return str(row["COLUMN_NAME"])

    def _get_incremental_column(self, table_name: str) -> str | None:
        rows = self.db.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND DATA_TYPE IN ('timestamp', 'datetime', 'date')
                ORDER BY ORDINAL_POSITION
                """
            ),
            {"schema_name": self.database_name, "table_name": table_name},
        ).mappings().all()
        if not rows:
            return None

        columns = [str(row["COLUMN_NAME"]) for row in rows]
        preferred = [
            "updated_at",
            "updatedon",
            "updated_on",
            "modified_at",
            "modifiedon",
            "modified_on",
            "last_updated",
            "lastupdated",
            "update_date",
            "updatedate",
            "edit_date",
            "editdate",
        ]
        by_lower = {col.lower(): col for col in columns}
        for candidate in preferred:
            found = by_lower.get(candidate)
            if found:
                return found
        return columns[0]

    def _get_cursor_key_expression(self, table_name: str, fallback_column: str) -> str:
        if table_name == "address":
            parts = [
                f"COALESCE(CAST(`{column}` AS CHAR), '<NULL>')"
                for column in self._ADDRESS_CURSOR_COLUMNS
            ]
            return f"CONCAT_WS('|', {', '.join(parts)})"
        return f"CAST(`{fallback_column}` AS CHAR)"

    def fetch_incremental(
        self,
        table_name: str,
        since: datetime,
        limit: int,
        cursor_updated_at: datetime | None,
        cursor_pk_value: str | None,
    ) -> SyncPage:
        incremental_col = self._get_incremental_column(table_name)

        pk_col = self._get_primary_key_column(table_name)
        tie_breaker_col = pk_col or self._get_fallback_order_column(table_name)
        if not tie_breaker_col:
            raise ValueError(f"No usable ordering column found for table '{table_name}'")
        cursor_key_expr = self._get_cursor_key_expression(table_name, tie_breaker_col)
        cursor_key_alias = self._CURSOR_KEY_ALIAS

        params: dict[str, Any] = {"since": since, "limit_plus": limit + 1}
        active_col = self._get_active_flag_column(table_name)
        if incremental_col:
            where_parts = [f"`{incremental_col}` > :since"]
            if active_col:
                where_parts.append(f"`{active_col}` = 1")
            if cursor_updated_at is not None and cursor_pk_value is not None:
                where_parts.append(
                    f"(`{incremental_col}` > :cursor_updated_at OR (`{incremental_col}` = :cursor_updated_at AND {cursor_key_expr} > :cursor_pk_value))"
                )
                params["cursor_updated_at"] = cursor_updated_at
                params["cursor_pk_value"] = cursor_pk_value
            query = text(
                f"""
                SELECT *, {cursor_key_expr} AS `{cursor_key_alias}`
                FROM `{table_name}`
                WHERE {' AND '.join(where_parts)}
                ORDER BY `{incremental_col}` ASC, {cursor_key_expr} ASC
                LIMIT :limit_plus
                """
            )
        else:
            where_parts = []
            if active_col:
                where_parts.append(f"`{active_col}` = 1")
            if cursor_pk_value is not None:
                where_parts.append(f"{cursor_key_expr} > :cursor_pk_value")
                params["cursor_pk_value"] = cursor_pk_value
            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            query = text(
                f"""
                SELECT *, {cursor_key_expr} AS `{cursor_key_alias}`
                FROM `{table_name}`
                {where_sql}
                ORDER BY {cursor_key_expr} ASC
                LIMIT :limit_plus
                """
            )
        rows = self.db.execute(query, params).mappings().all()

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = []
        for row in page_rows:
            item = dict(row)
            item.pop(cursor_key_alias, None)
            items.append(item)

        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            pk_value = last.get(cursor_key_alias)
            if incremental_col:
                updated_at = last.get(incremental_col)
                if updated_at is not None and pk_value is not None:
                    next_cursor = f"{updated_at.isoformat()}|{pk_value}"
            elif pk_value is not None:
                next_cursor = f"pk:{pk_value}"

        max_updated_at: datetime | None = None
        if page_rows and incremental_col:
            max_updated_at = page_rows[-1].get(incremental_col)

        return SyncPage(items=items, next_cursor=next_cursor, max_updated_at=max_updated_at)
