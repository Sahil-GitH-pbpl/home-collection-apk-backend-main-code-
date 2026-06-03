import json
from datetime import datetime, time, timedelta
from collections import defaultdict
from threading import Lock
from typing import Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, func
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.booking import (
    CallerMaster,
    HomeCollectionBooking,
    HomeCollectionBookingPatient,
    PatientMaster,
)


class BookingRepository:
    _appointment_index_ensured: bool = False
    _shared_table_columns_cache: dict[str, set[str]] = {}
    _shared_table_columns_lock = Lock()
    _ist_tz = ZoneInfo("Asia/Kolkata")

    def __init__(self, db: Session) -> None:
        self.db = db

    def _get_table_columns(self, table_name: str) -> set[str]:
        if table_name not in {
            "haddress_master",
            "hcaller_master",
            "hhome_collection_booking",
            "hhome_collection_booking_appointment",
            "hhome_collection_batch",
            "hhome_collection_booking_patient",
            "hhome_collection_booking_patient_test",
            "hpatient_master",
        }:
            raise ValueError(f"Unsupported table name: {table_name}")
        cached_columns = BookingRepository._shared_table_columns_cache.get(table_name)
        if cached_columns is not None:
            return cached_columns
        with BookingRepository._shared_table_columns_lock:
            cached_columns = BookingRepository._shared_table_columns_cache.get(table_name)
            if cached_columns is not None:
                return cached_columns
            rows = self.db.execute(text(f"SHOW COLUMNS FROM {table_name}")).fetchall()
            columns = {str(row.Field) for row in rows}
            BookingRepository._shared_table_columns_cache[table_name] = columns
            return columns

    def get_my_assigned_bookings(
        self,
        user_id: int,
        exclude_cancelled: bool = True,
        status_filter: Sequence[int] | None = None,
    ):
        query = (
            self.db.query(
                HomeCollectionBooking.id,
                HomeCollectionBooking.booking_code,
                HomeCollectionBooking.preferred_visit_date,
                HomeCollectionBooking.preferred_time_slot,
                HomeCollectionBooking.address_snapshot_json,
                HomeCollectionBooking.booking_status,
                CallerMaster.full_name.label("caller_name"),
                CallerMaster.primary_mobile.label("caller_mobile"),
                func.count(HomeCollectionBookingPatient.patient_id).label("patient_count"),
            )
            .outerjoin(CallerMaster, HomeCollectionBooking.caller_id == CallerMaster.id)
            .outerjoin(
                HomeCollectionBookingPatient,
                HomeCollectionBooking.id == HomeCollectionBookingPatient.booking_id,
            )
            .filter(HomeCollectionBooking.assigned_phlebotomist_id == user_id)
            .group_by(
                HomeCollectionBooking.id,
                HomeCollectionBooking.booking_code,
                HomeCollectionBooking.preferred_visit_date,
                HomeCollectionBooking.preferred_time_slot,
                HomeCollectionBooking.address_snapshot_json,
                HomeCollectionBooking.booking_status,
                CallerMaster.full_name,
                CallerMaster.primary_mobile,
            )
            .order_by(HomeCollectionBooking.id.desc())
        )
        if status_filter:
            query = query.filter(HomeCollectionBooking.booking_status.in_(list(status_filter)))
        if exclude_cancelled:
            query = query.filter(HomeCollectionBooking.booking_status != 3)
        return query.all()

    @staticmethod
    def _colony_name_from_snapshot(raw_snapshot: object) -> str | None:
        if raw_snapshot is None:
            return None
        try:
            snap = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
        except Exception:
            return None
        if not isinstance(snap, dict):
            return None
        for key in ("colony_name_snapshot", "colony_name"):
            value = str(snap.get(key) or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _to_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_status_code(cls, value: object) -> int | None:
        int_value = cls._to_int(value)
        if int_value is not None:
            return int_value
        text = str(value or "").strip().lower()
        mapping = {
            "assigned": 1,
            "pending": 0,
            "started": 2,
            "in_progress": 2,
            "inprogress": 2,
            "completed": 3,
            "cancelled": 4,
            "canceled": 4,
            "partial": 5,
        }
        return mapping.get(text)

    def _ensure_appointment_index(self) -> None:
        if BookingRepository._appointment_index_ensured:
            return
        try:
            exists = self.db.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = 'hhome_collection_booking_appointment'
                      AND index_name = 'idx_hc_appt_assigned_date_status'
                    LIMIT 1
                    """
                )
            ).fetchone()
            if not exists:
                self.db.execute(
                    text(
                        """
                        CREATE INDEX idx_hc_appt_assigned_date_status
                        ON hhome_collection_booking_appointment
                        (assigned_phlebotomist_id, preferred_visit_date, appointment_status)
                        """
                    )
                )
                self.db.commit()
            BookingRepository._appointment_index_ensured = True
        except Exception:
            self.db.rollback()

    def _get_appointment_columns(self) -> set[str]:
        try:
            return self._get_table_columns("hhome_collection_booking_appointment")
        except Exception:
            return set()

    def _get_booking_test_columns(self) -> set[str]:
        try:
            return self._get_table_columns("hhome_collection_booking_patient_test")
        except Exception:
            return set()

    @staticmethod
    def _build_patient_scope_where(
        patient_ids: Sequence[int] | None,
        params: dict[str, object],
        column_name: str = "patient_id",
    ) -> str:
        if not patient_ids:
            return ""
        cleaned_ids: list[int] = []
        seen: set[int] = set()
        for raw in patient_ids:
            parsed = BookingRepository._to_int(raw)
            if parsed is None or parsed <= 0 or parsed in seen:
                continue
            seen.add(parsed)
            cleaned_ids.append(parsed)
        if not cleaned_ids:
            return ""
        placeholders: list[str] = []
        for idx, pid in enumerate(cleaned_ids):
            key = f"scope_patient_id_{idx}"
            params[key] = pid
            placeholders.append(f":{key}")
        return f" AND {column_name} IN ({', '.join(placeholders)})"

    def _update_pending_tests_status(
        self,
        booking_id: int,
        to_status: int,
        patient_ids: Sequence[int] | None = None,
    ) -> None:
        test_cols = self._get_booking_test_columns()
        if "test_status" not in test_cols:
            return
        params: dict[str, object] = {
            "booking_id": booking_id,
            "from_status": 0,
            "to_status": to_status,
        }
        scope_sql = self._build_patient_scope_where(
            patient_ids=patient_ids,
            params=params,
            column_name="patient_id",
        )
        self.db.execute(
            text(
                f"""
                UPDATE hhome_collection_booking_patient_test
                SET test_status = :to_status
                WHERE booking_id = :booking_id
                  AND test_status = :from_status
                  {scope_sql}
                """
            ),
            params,
        )

    @staticmethod
    def _parse_selected_patient_ids(raw_value: object) -> list[int]:
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            values = raw_value
        else:
            text_value = str(raw_value).strip()
            if not text_value:
                return []
            try:
                parsed = json.loads(text_value)
                values = parsed if isinstance(parsed, list) else []
            except Exception:
                values = [x.strip() for x in text_value.split(",") if str(x).strip()]
        out: list[int] = []
        seen: set[int] = set()
        for value in values:
            parsed_int = BookingRepository._to_int(value)
            if parsed_int is None or parsed_int <= 0 or parsed_int in seen:
                continue
            seen.add(parsed_int)
            out.append(parsed_int)
        return out

    def get_appointment_selected_patient_ids(
        self,
        appointment_id: int,
        user_id: int | None = None,
    ) -> tuple[int | None, int | None, list[int], str]:
        cols = self._get_appointment_columns()
        if not cols:
            return None, None, [], "BOOKING_ALL_FALLBACK"
        id_col = "id" if "id" in cols else "appointment_id"
        selected_col = (
            "selected_patient_ids_json"
            if "selected_patient_ids_json" in cols
            else None
        )
        if not selected_col:
            return None, None, [], "BOOKING_ALL_FALLBACK"
        status_col = (
            "appointment_status"
            if "appointment_status" in cols
            else None
        )
        where_extra = ""
        params: dict[str, object] = {"appointment_id": appointment_id}
        if user_id is not None and "assigned_phlebotomist_id" in cols:
            where_extra = " AND assigned_phlebotomist_id = :user_id"
            params["user_id"] = user_id
        row = self.db.execute(
            text(
                f"""
                SELECT
                    booking_id,
                    {status_col} AS appointment_status,
                    {selected_col} AS selected_patient_ids_json
                FROM hhome_collection_booking_appointment
                WHERE {id_col} = :appointment_id
                {where_extra}
                LIMIT 1
                """
            ),
            params,
        ).mappings().first()
        if not row:
            return None, None, [], "BOOKING_ALL_FALLBACK"
        booking_id = self._to_int(row.get("booking_id"))
        appointment_status = self._normalize_status_code(row.get("appointment_status"))
        selected_ids = self._parse_selected_patient_ids(row.get("selected_patient_ids_json"))
        scope = "APPOINTMENT_SELECTED" if selected_ids else "BOOKING_ALL_FALLBACK"
        return booking_id, appointment_status, selected_ids, scope

    def update_appointment_selected_patient_ids(
        self,
        appointment_id: int,
        selected_patient_ids: Sequence[int],
    ) -> None:
        cols = self._get_appointment_columns()
        if not cols:
            return
        id_col = "id" if "id" in cols else "appointment_id"
        if "selected_patient_ids_json" not in cols:
            return
        cleaned = self._parse_selected_patient_ids(list(selected_patient_ids))
        self.db.execute(
            text(
                f"""
                UPDATE hhome_collection_booking_appointment
                SET selected_patient_ids_json = :selected_patient_ids_json
                WHERE {id_col} = :appointment_id
                """
            ),
            {
                "selected_patient_ids_json": json.dumps(cleaned, ensure_ascii=True),
                "appointment_id": appointment_id,
            },
        )

    def get_my_assigned_appointments(
        self,
        user_id: int,
        status_filter: Sequence[int] | None = None,
        include_terminal: bool = False,
    ) -> list[dict]:
        self._ensure_appointment_index()
        cols = self._get_appointment_columns()
        if not cols:
            return []
        if "assigned_phlebotomist_id" not in cols or "booking_id" not in cols:
            return []
        id_col = "id" if "id" in cols else "appointment_id"
        route_col = "route_no" if "route_no" in cols else ("route" if "route" in cols else None)
        assign_col = (
            "assigned_at"
            if "assigned_at" in cols
            else ("assign_time" if "assign_time" in cols else None)
        )
        slot_col = (
            "preferred_time_slot"
            if "preferred_time_slot" in cols
            else ("preferred_slot" if "preferred_slot" in cols else None)
        )
        date_col = (
            "preferred_visit_date"
            if "preferred_visit_date" in cols
            else ("visit_date" if "visit_date" in cols else None)
        )

        select_parts = [
            f"a.{id_col} AS appointment_id",
            "a.booking_id AS booking_id",
            "a.appointment_no AS appointment_no" if "appointment_no" in cols else "NULL AS appointment_no",
            "a.appointment_status AS appointment_status" if "appointment_status" in cols else "NULL AS appointment_status",
            "a.selected_patient_ids_json AS selected_patient_ids_json" if "selected_patient_ids_json" in cols else "NULL AS selected_patient_ids_json",
            f"a.{date_col} AS preferred_visit_date" if date_col else "NULL AS preferred_visit_date",
            f"a.{slot_col} AS preferred_time_slot" if slot_col else "NULL AS preferred_time_slot",
            f"a.{route_col} AS route" if route_col else "NULL AS route",
            f"a.{assign_col} AS assign_time" if assign_col else "NULL AS assign_time",
            "c.full_name AS caller_name",
            "c.primary_mobile AS caller_mobile",
            "COUNT(bp.patient_id) AS booking_patient_count",
        ]

        rows = self.db.execute(
            text(
                f"""
                SELECT
                    {", ".join(select_parts)}
                FROM hhome_collection_booking_appointment a
                LEFT JOIN hhome_collection_booking b ON b.id = a.booking_id
                LEFT JOIN hcaller_master c ON c.id = b.caller_id
                LEFT JOIN hhome_collection_booking_patient bp ON bp.booking_id = a.booking_id
                WHERE a.assigned_phlebotomist_id = :user_id
                GROUP BY
                    appointment_id,
                    a.booking_id,
                    appointment_no,
                    appointment_status,
                    selected_patient_ids_json,
                    preferred_visit_date,
                    preferred_time_slot,
                    route,
                    assign_time,
                    c.full_name,
                    c.primary_mobile
                """
            ),
            {"user_id": user_id},
        ).mappings().all()

        out: list[dict] = []
        for row in rows:
            status = self._normalize_status_code(row.get("appointment_status"))
            if status_filter and status not in status_filter:
                continue
            if not include_terminal and status in {3, 4, 5}:
                continue
            selected_ids = self._parse_selected_patient_ids(row.get("selected_patient_ids_json"))
            patient_scope = "APPOINTMENT_SELECTED" if selected_ids else "BOOKING_ALL_FALLBACK"
            patient_count = len(selected_ids) if selected_ids else int(row.get("booking_patient_count") or 0)
            out.append(
                {
                    "source_type": "APPOINTMENT",
                    "id": int(row["booking_id"]) if row.get("booking_id") else int(row["appointment_id"]),
                    "booking_id": int(row["booking_id"]) if row.get("booking_id") is not None else None,
                    "appointment_id": int(row["appointment_id"]) if row.get("appointment_id") is not None else None,
                    "appointment_no": row.get("appointment_no"),
                    "booking_status": status if status is not None else 0,
                    "preferred_visit_date": row.get("preferred_visit_date"),
                    "preferred_time_slot": row.get("preferred_time_slot"),
                    "caller_name": row.get("caller_name"),
                    "caller_mobile": row.get("caller_mobile"),
                    "patient_count": patient_count,
                    "patient_scope": patient_scope,
                    "route": row.get("route"),
                    "assign_time": str(row.get("assign_time")) if row.get("assign_time") is not None else None,
                }
            )
        return out

    def get_patient_names_by_booking_ids(self, booking_ids: Sequence[int]) -> dict[int, str]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for raw in booking_ids:
            pid = self._to_int(raw)
            if pid is None or pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            cleaned.append(pid)
        if not cleaned:
            return {}

        rows = self.db.execute(
            text(
                """
                SELECT bp.booking_id, p.title, p.full_name, p.id AS patient_id
                FROM hhome_collection_booking_patient bp
                INNER JOIN hpatient_master p ON p.id = bp.patient_id
                WHERE bp.booking_id IN :booking_ids
                ORDER BY bp.booking_id, bp.id, p.id
                """
            ).bindparams(bindparam("booking_ids", expanding=True)),
            {"booking_ids": cleaned},
        ).mappings().all()

        grouped: dict[int, list[str]] = defaultdict(list)
        for row in rows:
            booking_id = self._to_int(row.get("booking_id"))
            if booking_id is None:
                continue
            title = str(row.get("title") or "").strip()
            full_name = str(row.get("full_name") or "").strip()
            name = f"{title} {full_name}".strip() if title else full_name
            if not name:
                continue
            grouped[booking_id].append(name)

        return {bid: ", ".join(names) for bid, names in grouped.items()}

    @staticmethod
    def _within_4am_window_for_date(visit_date: object, now_dt: datetime | None = None) -> bool:
        if visit_date is None:
            return False
        if now_dt is None:
            now_dt = datetime.now(BookingRepository._ist_tz)
        elif now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=BookingRepository._ist_tz)
        else:
            now_dt = now_dt.astimezone(BookingRepository._ist_tz)
        if hasattr(visit_date, 'year') and hasattr(visit_date, 'month') and hasattr(visit_date, 'day'):
            d = visit_date
        else:
            try:
                d = datetime.strptime(str(visit_date), "%Y-%m-%d").date()
            except Exception:
                return False
        start_dt = datetime.combine(d, time(4, 0, 0), tzinfo=BookingRepository._ist_tz)
        end_dt = start_dt + timedelta(days=1)
        return start_dt <= now_dt < end_dt

    @staticmethod
    def _current_assigned_visit_date(now_dt: datetime | None = None):
        if now_dt is None:
            current_dt = datetime.now(BookingRepository._ist_tz)
        elif now_dt.tzinfo is None:
            current_dt = now_dt.replace(tzinfo=BookingRepository._ist_tz)
        else:
            current_dt = now_dt.astimezone(BookingRepository._ist_tz)
        if current_dt.time() < time(4, 0, 0):
            return (current_dt - timedelta(days=1)).date()
        return current_dt.date()

    def get_my_assigned_merged(
        self,
        user_id: int,
        status_filter: Sequence[int] | None,
        include_terminal: bool,
        limit: int,
        offset: int,
    ) -> list[dict]:
        appointment_cols = self._get_appointment_columns()
        target_visit_date = self._current_assigned_visit_date()
        status_values = [
            self._to_int(value)
            for value in (status_filter or [])
            if self._to_int(value) is not None
        ]
        booking_status_sql = ""
        appointment_status_sql = ""
        params: dict[str, object] = {
            "user_id": user_id,
            "target_visit_date": target_visit_date,
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        appointment_id_col = "id" if "id" in appointment_cols else "appointment_id"
        appointment_route_sql = (
            "COALESCE(a.route_no, '')"
            if "route_no" in appointment_cols
            else ("COALESCE(a.route, '')" if "route" in appointment_cols else "''")
        )
        appointment_slot_col = (
            "preferred_time_slot"
            if "preferred_time_slot" in appointment_cols
            else ("preferred_slot" if "preferred_slot" in appointment_cols else None)
        )
        appointment_date_col = (
            "preferred_visit_date"
            if "preferred_visit_date" in appointment_cols
            else ("visit_date" if "visit_date" in appointment_cols else None)
        )
        if status_values:
            status_placeholders: list[str] = []
            for index, status_value in enumerate(status_values):
                key = f"status_{index}"
                params[key] = status_value
                status_placeholders.append(f":{key}")
            status_sql = ", ".join(status_placeholders)
            booking_status_sql = f" AND b.booking_status IN ({status_sql})"
            if "appointment_status" in appointment_cols:
                appointment_status_sql = f" AND a.appointment_status IN ({status_sql})"
        if not include_terminal:
            booking_status_sql += " AND b.booking_status != 3"
            if "appointment_status" in appointment_cols:
                appointment_status_sql += " AND COALESCE(a.appointment_status, 0) NOT IN (3, 4, 5)"

        appointment_query = """
                SELECT
                    'APPOINTMENT' AS source_type,
                    a.booking_id AS id,
                    a.booking_id AS booking_id,
                    a.{appointment_id_col} AS appointment_id,
                    a.appointment_no AS appointment_no,
                    COALESCE(a.appointment_status, 0) AS booking_status,
                    a.{appointment_date_col} AS preferred_visit_date,
                    a.{appointment_slot_col} AS preferred_time_slot,
                    c.primary_mobile AS caller_mobile,
                    CASE
                        WHEN JSON_VALID(a.selected_patient_ids_json) AND JSON_LENGTH(a.selected_patient_ids_json) > 0
                            THEN JSON_LENGTH(a.selected_patient_ids_json)
                        ELSE COUNT(DISTINCT bp.patient_id)
                    END AS patient_count,
                    CASE
                        WHEN JSON_VALID(a.selected_patient_ids_json) AND JSON_LENGTH(a.selected_patient_ids_json) > 0
                            THEN 'APPOINTMENT_SELECTED'
                        ELSE 'BOOKING_ALL_FALLBACK'
                    END AS patient_scope,
                    {appointment_route_sql} AS route,
                    CASE
                        WHEN NULLIF(TRIM(COALESCE(b.booking_tags, '')), '') IS NOT NULL
                          OR SUM(CASE WHEN NULLIF(TRIM(COALESCE(p.tag, '')), '') IS NOT NULL THEN 1 ELSE 0 END) > 0
                        THEN 1 ELSE 0
                    END AS tag,
                    GROUP_CONCAT(
                        DISTINCT TRIM(CONCAT(COALESCE(p.title, ''), ' ', COALESCE(p.full_name, '')))
                        ORDER BY bp.id, p.id
                        SEPARATOR ', '
                    ) AS patient_names
                FROM hhome_collection_booking_appointment a
                LEFT JOIN hhome_collection_booking b ON b.id = a.booking_id
                LEFT JOIN hcaller_master c ON c.id = b.caller_id
                LEFT JOIN hhome_collection_booking_patient bp ON bp.booking_id = a.booking_id
                LEFT JOIN hpatient_master p ON p.id = bp.patient_id
                WHERE a.assigned_phlebotomist_id = :user_id
                  AND a.{appointment_date_col} = :target_visit_date
                  {appointment_status_sql}
                GROUP BY
                    a.booking_id,
                    a.{appointment_id_col},
                    a.appointment_no,
                    a.appointment_status,
                    a.selected_patient_ids_json,
                    a.{appointment_date_col},
                    a.{appointment_slot_col},
                    route,
                    b.booking_tags,
                    c.primary_mobile
        """.format(
            appointment_id_col=appointment_id_col,
            appointment_date_col=appointment_date_col,
            appointment_slot_col=appointment_slot_col,
            appointment_route_sql=appointment_route_sql,
            appointment_status_sql=appointment_status_sql,
        )
        if "assigned_phlebotomist_id" not in appointment_cols or "booking_id" not in appointment_cols:
            appointment_query = """
                SELECT
                    'APPOINTMENT' AS source_type,
                    NULL AS id,
                    NULL AS booking_id,
                    NULL AS appointment_id,
                    NULL AS appointment_no,
                    NULL AS booking_status,
                    NULL AS preferred_visit_date,
                    NULL AS preferred_time_slot,
                    NULL AS caller_mobile,
                    0 AS patient_count,
                    'BOOKING_ALL_FALLBACK' AS patient_scope,
                    '' AS route,
                    0 AS tag,
                    NULL AS patient_names
                WHERE 1 = 0
            """
        elif appointment_date_col is None or appointment_slot_col is None:
            appointment_query = """
                SELECT
                    'APPOINTMENT' AS source_type,
                    NULL AS id,
                    NULL AS booking_id,
                    NULL AS appointment_id,
                    NULL AS appointment_no,
                    NULL AS booking_status,
                    NULL AS preferred_visit_date,
                    NULL AS preferred_time_slot,
                    NULL AS caller_mobile,
                    0 AS patient_count,
                    'BOOKING_ALL_FALLBACK' AS patient_scope,
                    '' AS route,
                    0 AS tag,
                    NULL AS patient_names
                WHERE 1 = 0
            """

        rows = self.db.execute(
            text(
                f"""
                SELECT *
                FROM (
                    SELECT
                        'BOOKING' AS source_type,
                        b.id AS id,
                        b.id AS booking_id,
                        NULL AS appointment_id,
                        NULL AS appointment_no,
                        b.booking_status AS booking_status,
                        b.preferred_visit_date AS preferred_visit_date,
                        b.preferred_time_slot AS preferred_time_slot,
                        c.primary_mobile AS caller_mobile,
                        COUNT(DISTINCT bp.patient_id) AS patient_count,
                        'BOOKING_ALL_FALLBACK' AS patient_scope,
                        CASE
                            WHEN JSON_VALID(b.address_snapshot_json)
                                THEN COALESCE(
                                    JSON_UNQUOTE(JSON_EXTRACT(b.address_snapshot_json, '$.colony_name_snapshot')),
                                    JSON_UNQUOTE(JSON_EXTRACT(b.address_snapshot_json, '$.colony_name')),
                                    ''
                                )
                            ELSE ''
                        END AS route,
                        CASE
                            WHEN NULLIF(TRIM(COALESCE(b.booking_tags, '')), '') IS NOT NULL
                              OR SUM(CASE WHEN NULLIF(TRIM(COALESCE(p.tag, '')), '') IS NOT NULL THEN 1 ELSE 0 END) > 0
                            THEN 1 ELSE 0
                        END AS tag,
                        GROUP_CONCAT(
                            DISTINCT TRIM(CONCAT(COALESCE(p.title, ''), ' ', COALESCE(p.full_name, '')))
                            ORDER BY bp.id, p.id
                            SEPARATOR ', '
                        ) AS patient_names
                    FROM hhome_collection_booking b
                    LEFT JOIN hcaller_master c ON c.id = b.caller_id
                    LEFT JOIN hhome_collection_booking_patient bp ON bp.booking_id = b.id
                    LEFT JOIN hpatient_master p ON p.id = bp.patient_id
                    WHERE b.assigned_phlebotomist_id = :user_id
                      AND b.preferred_visit_date = :target_visit_date
                      {booking_status_sql}
                    GROUP BY
                        b.id,
                        b.booking_status,
                        b.preferred_visit_date,
                        b.preferred_time_slot,
                        c.primary_mobile,
                        route,
                        b.booking_tags

                    UNION ALL

                    {appointment_query}
                ) merged
                ORDER BY
                    merged.preferred_visit_date ASC,
                    merged.preferred_time_slot ASC,
                    merged.booking_id ASC,
                    merged.appointment_id ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [
            {
                "source_type": str(row.get("source_type") or "BOOKING"),
                "id": self._to_int(row.get("id")),
                "booking_id": self._to_int(row.get("booking_id")),
                "appointment_id": self._to_int(row.get("appointment_id")),
                "appointment_no": row.get("appointment_no"),
                "booking_status": self._normalize_status_code(row.get("booking_status")),
                "preferred_visit_date": row.get("preferred_visit_date"),
                "preferred_time_slot": row.get("preferred_time_slot"),
                "caller_mobile": row.get("caller_mobile"),
                "patient_count": int(row.get("patient_count") or 0),
                "patient_scope": str(row.get("patient_scope") or "BOOKING_ALL_FALLBACK"),
                "route": str(row.get("route") or "").strip() or None,
                "patient_names": str(row.get("patient_names") or "").strip() or None,
                "tag": int(row.get("tag") or 0),
            }
            for row in rows
        ]

    def get_assigned_booking_by_id(
        self, booking_id: int, user_id: int, exclude_cancelled: bool = True
    ) -> HomeCollectionBooking | None:
        query = self.db.query(HomeCollectionBooking).filter(
            HomeCollectionBooking.id == booking_id,
            HomeCollectionBooking.assigned_phlebotomist_id == user_id,
        )
        if exclude_cancelled:
            query = query.filter(HomeCollectionBooking.booking_status != 3)
        return query.first()

    def get_booking_by_id(self, booking_id: int) -> HomeCollectionBooking | None:
        return self.db.query(HomeCollectionBooking).filter(HomeCollectionBooking.id == booking_id).first()

    def get_caller(self, caller_id: int | None) -> CallerMaster | None:
        if not caller_id:
            return None

    @staticmethod
    def _clean_text(value: object) -> str | None:
        text_value = str(value or "").strip()
        return text_value or None

    def _apply_booking_lifecycle_fields(
        self,
        *,
        booking_id: int,
        action: str,
        start_time: str | None = None,
        start_location: str | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> None:
        cols = self._get_table_columns("hhome_collection_booking")
        sets: list[str] = []
        params: dict[str, object] = {"booking_id": int(booking_id)}

        if action == "start":
            if "start_time" in cols and self._clean_text(start_time):
                sets.append("start_time=:start_time")
                params["start_time"] = self._clean_text(start_time)
            if "start_location" in cols and self._clean_text(start_location):
                sets.append("start_location=:start_location")
                params["start_location"] = self._clean_text(start_location)
        elif action in {"complete", "cancel"}:
            if "complete_time" in cols and self._clean_text(complete_time):
                sets.append("complete_time=:complete_time")
                params["complete_time"] = self._clean_text(complete_time)
            if "complete_location" in cols and self._clean_text(complete_location):
                sets.append("complete_location=:complete_location")
                params["complete_location"] = self._clean_text(complete_location)

        if sets:
            self.db.execute(
                text(
                    f"""
                    UPDATE hhome_collection_booking
                    SET {', '.join(sets)}
                    WHERE id=:booking_id
                    """
                ),
                params,
            )

    def _apply_appointment_lifecycle_fields(
        self,
        *,
        appointment_id: int,
        booking_id: int,
        action: str,
        start_time: str | None = None,
        start_location: str | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> None:
        cols = self._get_appointment_columns()
        if not cols:
            return
        id_col = "id" if "id" in cols else "appointment_id"
        sets: list[str] = []
        params: dict[str, object] = {
            "appointment_id": int(appointment_id),
            "booking_id": int(booking_id),
        }

        if action == "start":
            if "start_time" in cols and self._clean_text(start_time):
                sets.append("start_time=:start_time")
                params["start_time"] = self._clean_text(start_time)
            if "start_location" in cols and self._clean_text(start_location):
                sets.append("start_location=:start_location")
                params["start_location"] = self._clean_text(start_location)
        elif action in {"complete", "cancel"}:
            if "complete_time" in cols and self._clean_text(complete_time):
                sets.append("complete_time=:complete_time")
                params["complete_time"] = self._clean_text(complete_time)
            if "complete_location" in cols and self._clean_text(complete_location):
                sets.append("complete_location=:complete_location")
                params["complete_location"] = self._clean_text(complete_location)

        if sets:
            self.db.execute(
                text(
                    f"""
                    UPDATE hhome_collection_booking_appointment
                    SET {', '.join(sets)}
                    WHERE {id_col}=:appointment_id
                      AND booking_id=:booking_id
                    """
                ),
                params,
            )


    def get_address(self, address_id: int | None) -> dict | None:
        if not address_id:
            return None
        expected_columns = [
            "id",
            "address_type",
            "house_flat_no",
            "floor",
            "block_tower_no",
            "street_line",
            "landmark",
            "colony_name",
            "pincode",
            "google_location",
            "route_no",
            "city",
            "access_notes",
        ]
        available_columns = self._get_table_columns("haddress_master")
        selected_columns = [col for col in expected_columns if col in available_columns]
        if not selected_columns:
            return None

        sql_columns = ", ".join(selected_columns)
        row = self.db.execute(
            text(
                f"""
                SELECT {sql_columns}
                FROM haddress_master
                WHERE id = :address_id
                LIMIT 1
                """
            ),
            {"address_id": address_id},
        ).mappings().first()
        if not row:
            return None
        address = {col: row.get(col) for col in expected_columns}
        address["colony_name_snapshot"] = row.get("colony_name")
        address["pincode_snapshot"] = row.get("pincode")
        address["route_no_snapshot"] = row.get("route_no")
        address["pincode"] = row.get("pincode")
        address["location_url"] = row.get("google_location")
        return address

    def get_patients_for_booking(self, booking_id: int, patient_ids: Sequence[int] | None = None):
        query = (
            self.db.query(
                HomeCollectionBookingPatient.id.label("booking_patient_id"),
                HomeCollectionBookingPatient.booking_patient_status,
                HomeCollectionBookingPatient.cce_level_TBS,
                HomeCollectionBookingPatient.selected_comp_cat_ids,
                HomeCollectionBookingPatient.referred_by,
                HomeCollectionBookingPatient.selected_charge_modes,
                HomeCollectionBookingPatient.selected_panel_companies,
                HomeCollectionBookingPatient.additional_discount_amount,
                HomeCollectionBookingPatient.payment_mode,
                HomeCollectionBookingPatient.due_amount,
                HomeCollectionBookingPatient.extra_amount,
                HomeCollectionBookingPatient.prescription_files.label("bp_prescription_files"),
                PatientMaster,
            )
            .join(
                HomeCollectionBookingPatient,
                HomeCollectionBookingPatient.patient_id == PatientMaster.id,
            )
            .filter(HomeCollectionBookingPatient.booking_id == booking_id)
        )
        if patient_ids:
            query = query.filter(HomeCollectionBookingPatient.patient_id.in_(list(patient_ids)))
        rows = query.order_by(PatientMaster.id.asc()).all()
        return rows

    def get_linked_patients_for_caller(
        self,
        caller_id: int,
        exclude_patient_ids: Sequence[int] | None = None,
    ):
        params: dict[str, object] = {"caller_id": caller_id}
        where_extra = ""
        if exclude_patient_ids:
            placeholders = []
            for idx, pid in enumerate(exclude_patient_ids):
                key = f"pid_{idx}"
                placeholders.append(f":{key}")
                params[key] = int(pid)
            if placeholders:
                where_extra = f" AND p.id NOT IN ({', '.join(placeholders)})"
        return self.db.execute(
            text(
                f"""
                SELECT p.*
                FROM hcaller_patient_link cpl
                INNER JOIN hpatient_master p ON p.id = cpl.patient_id
                WHERE cpl.caller_id = :caller_id
                  AND cpl.is_active = 1
                  {where_extra}
                ORDER BY p.id
                """
            ),
            params,
        ).mappings().all()

    def _appointment_payment_snapshot_obj(self, raw_value) -> dict:
        if isinstance(raw_value, dict):
            return raw_value
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else {}
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _build_appointment_patient_context(
        self,
        booking_id: int,
        patient_ids: Sequence[int] | None,
        default_status: int | None = None,
        existing_context: dict | None = None,
    ) -> dict:
        ids = sorted({int(x) for x in (patient_ids or []) if int(x or 0) > 0})
        out = dict(existing_context or {}) if isinstance(existing_context, dict) else {}
        if not ids:
            return out
        rows = self.db.execute(
            text(
                """
                SELECT patient_id, booking_patient_status, payment_mode, due_amount, extra_amount, additional_discount_amount
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id
                  AND patient_id IN :patient_ids
                """
            ).bindparams(bindparam("patient_ids", expanding=True)),
            {"booking_id": int(booking_id), "patient_ids": ids},
        ).mappings().all()
        for row in rows:
            pid = int(row.get("patient_id") or 0)
            if pid <= 0:
                continue
            existing = out.get(str(pid)) if isinstance(out.get(str(pid)), dict) else {}
            out[str(pid)] = {
                "appointment_patient_status": int(existing.get("appointment_patient_status") if existing.get("appointment_patient_status") is not None else (default_status if default_status is not None else 0)),
                "booking_due_amount": float(row.get("due_amount") or 0),
                "booking_extra_amount": float(row.get("extra_amount") or 0),
                "booking_payment_mode": str(row.get("payment_mode") or "").strip() or None,
                "booking_additional_discount_amount": float(row.get("additional_discount_amount") or 0),
                "appointment_additional_discount_amount": float(existing.get("appointment_additional_discount_amount") or 0),
            }
        return out

    def _save_appointment_patient_context(
        self,
        booking_id: int,
        appointment_id: int,
        patient_ids: Sequence[int] | None,
        status_value: int | None = None,
    ) -> None:
        appt_cols = self._get_appointment_columns()
        if "payment_snapshot_json" not in appt_cols:
            return
        row = self.db.execute(
            text(
                """
                SELECT payment_snapshot_json
                FROM hhome_collection_booking_appointment
                WHERE id = :appointment_id AND booking_id = :booking_id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"appointment_id": int(appointment_id), "booking_id": int(booking_id)},
        ).mappings().first()
        if not row:
            return
        payload = self._appointment_payment_snapshot_obj(row.get("payment_snapshot_json"))
        if not isinstance(payload.get("payments"), list):
            payload["payments"] = []
        if not isinstance(payload.get("payment_screenshots"), dict):
            payload["payment_screenshots"] = {}
        if not isinstance(payload.get("summary"), dict):
            payload["summary"] = {}
        existing_ctx = payload.get("patient_context") if isinstance(payload.get("patient_context"), dict) else {}
        merged_ctx = self._build_appointment_patient_context(
            booking_id=booking_id,
            patient_ids=patient_ids,
            default_status=status_value if status_value is not None else None,
            existing_context=existing_ctx,
        )
        if status_value is not None:
            for pid in [int(x) for x in (patient_ids or []) if int(x or 0) > 0]:
                node = merged_ctx.get(str(pid)) if isinstance(merged_ctx.get(str(pid)), dict) else {}
                node["appointment_patient_status"] = int(status_value)
                merged_ctx[str(pid)] = node
        payload["patient_context"] = merged_ctx
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_appointment
                SET payment_snapshot_json = :snapshot_json,
                    updated_at = NOW()
                WHERE id = :appointment_id AND booking_id = :booking_id
                """
            ),
            {
                "snapshot_json": json.dumps(payload, ensure_ascii=False),
                "appointment_id": int(appointment_id),
                "booking_id": int(booking_id),
            },
        )

    def get_appointment_tests_snapshot(
        self,
        appointment_id: int,
        user_id: int,
    ) -> str | None:
        appt_cols = self._get_appointment_columns()
        if "appointment_tests_snapshot_json" not in appt_cols:
            return None
        row = self.db.execute(
            text(
                """
                SELECT appointment_tests_snapshot_json
                FROM hhome_collection_booking_appointment
                WHERE id = :appointment_id
                  AND assigned_phlebotomist_id = :user_id
                LIMIT 1
                """
            ),
            {"appointment_id": int(appointment_id), "user_id": int(user_id)},
        ).mappings().first()
        if not row:
            return None
        snapshot = row.get("appointment_tests_snapshot_json")
        return str(snapshot) if snapshot is not None else None

    def save_appointment_tests_snapshot(
        self,
        booking_id: int,
        appointment_id: int,
        user_id: int,
        snapshot_payload: dict,
    ) -> None:
        appt_cols = self._get_appointment_columns()
        if "appointment_tests_snapshot_json" not in appt_cols:
            return
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_appointment
                SET appointment_tests_snapshot_json = :snapshot_json,
                    updated_at = NOW()
                WHERE id = :appointment_id
                  AND booking_id = :booking_id
                  AND assigned_phlebotomist_id = :user_id
                """
            ),
            {
                "snapshot_json": json.dumps(snapshot_payload, ensure_ascii=False),
                "appointment_id": int(appointment_id),
                "booking_id": int(booking_id),
                "user_id": int(user_id),
            },
        )
        self.db.commit()

    def save_appointment_payment_snapshot(
        self,
        booking_id: int,
        appointment_id: int,
        snapshot_payload: dict,
    ) -> None:
        appt_cols = self._get_appointment_columns()
        if "payment_snapshot_json" not in appt_cols:
            return
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_appointment
                SET payment_snapshot_json = :snapshot_json,
                    updated_at = NOW()
                WHERE id = :appointment_id
                  AND booking_id = :booking_id
                """
            ),
            {
                "snapshot_json": json.dumps(snapshot_payload or {}, ensure_ascii=False),
                "appointment_id": int(appointment_id),
                "booking_id": int(booking_id),
            },
        )
        self.db.commit()

    def get_tests_for_booking(
        self,
        booking_id: int,
        patient_ids: Sequence[int] | None = None,
        pending_only: bool = False,
    ):
        test_cols = self._get_booking_test_columns()
        select_parts = [
            "patient_id",
            "booked_code",
            "test_name",
            "mrp",
            "charge",
            "max_discount",
        ]
        if "comp_cat_id" in test_cols:
            select_parts.append("comp_cat_id")
        else:
            select_parts.append("NULL AS comp_cat_id")
        if "test_status" in test_cols:
            select_parts.append("test_status")
        else:
            select_parts.append("NULL AS test_status")
        params: dict[str, object] = {"booking_id": booking_id}
        where_parts = ["booking_id = :booking_id"]
        patient_scope_sql = self._build_patient_scope_where(
            patient_ids=patient_ids,
            params=params,
            column_name="patient_id",
        )
        if patient_scope_sql:
            where_parts.append(patient_scope_sql.removeprefix(" AND "))
        if pending_only and "test_status" in test_cols:
            where_parts.append("test_status = 0")
        rows = self.db.execute(
            text(
                f"""
                SELECT {", ".join(select_parts)}
                FROM hhome_collection_booking_patient_test
                WHERE {" AND ".join(where_parts)}
                ORDER BY patient_id ASC
                """
            ),
            params,
        ).mappings().all()

        # Map (patient_id, comp_cat_id) -> panel_company using booking-patient CSV snapshots.
        panel_map_rows = self.db.execute(
            text(
                """
                SELECT patient_id, selected_comp_cat_ids, selected_panel_companies
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id
                """
            ),
            {"booking_id": booking_id},
        ).mappings().all()
        panel_map: dict[tuple[int, str], str] = {}
        for prow in panel_map_rows:
            pid = self._to_int(prow.get("patient_id"))
            if pid is None or pid <= 0:
                continue
            comp_ids = [x.strip() for x in str(prow.get("selected_comp_cat_ids") or "").split(",") if x and x.strip()]
            panels = [x.strip() for x in str(prow.get("selected_panel_companies") or "").split(",") if x and x.strip()]
            for idx, comp in enumerate(comp_ids):
                if idx < len(panels):
                    panel_map[(int(pid), comp)] = panels[idx]

        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in rows:
            pid = int(row["patient_id"])
            comp_cat_id = str(row.get("comp_cat_id") or "").strip() or None
            panel_company = panel_map.get((pid, comp_cat_id or "")) if comp_cat_id else None
            grouped[pid].append(
                {
                    "booked_code": row.get("booked_code"),
                    "comp_cat_id": comp_cat_id,
                    "panel_company": panel_company,
                    "test_name": row.get("test_name"),
                    "test_status": self._to_int(row.get("test_status")),
                    "mrp": float(row.get("mrp") or 0),
                    "charge": float(row.get("charge") or 0),
                    "max_discount": float(row.get("max_discount") or 0),
                }
            )
        return grouped

    def update_booking_status(
        self, booking: HomeCollectionBooking, new_status: int
    ) -> HomeCollectionBooking:
        booking.booking_status = new_status
        self.db.add(booking)
        self.db.commit()
        self.db.refresh(booking)
        return booking

    def get_booking_patient_status_rows(self, booking_id: int) -> list[dict]:
        rows = self.get_booking_patient_status_rows_filtered(booking_id=booking_id, patient_ids=None)
        return rows

    def get_booking_patient_status_rows_filtered(
        self,
        booking_id: int,
        patient_ids: Sequence[int] | None,
    ) -> list[dict]:
        query = (
            self.db.query(
                HomeCollectionBookingPatient.id.label("booking_patient_id"),
                HomeCollectionBookingPatient.patient_id,
                HomeCollectionBookingPatient.booking_patient_status,
            )
            .filter(HomeCollectionBookingPatient.booking_id == booking_id)
            .order_by(HomeCollectionBookingPatient.id.asc())
        )
        if patient_ids:
            query = query.filter(HomeCollectionBookingPatient.patient_id.in_(list(patient_ids)))
        rows = query.all()
        return [
            {
                "booking_patient_id": int(row.booking_patient_id),
                "patient_id": int(row.patient_id),
                "booking_patient_status": int(row.booking_patient_status or 0),
            }
            for row in rows
        ]

    def get_booking_patient_status_counts(self, booking_id: int) -> dict[int, int]:
        rows = self.db.execute(
            text(
                """
                SELECT booking_patient_status, COUNT(*) AS cnt
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id
                GROUP BY booking_patient_status
                """
            ),
            {"booking_id": booking_id},
        ).fetchall()
        counts: dict[int, int] = {}
        for row in rows:
            counts[int(row.booking_patient_status or 0)] = int(row.cnt)
        return counts

    def booking_has_patients(self, booking_id: int) -> bool:
        row = self.db.execute(
            text(
                """
                SELECT 1
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id
                LIMIT 1
                """
            ),
            {"booking_id": booking_id},
        ).fetchone()
        return bool(row)

    def apply_booking_action(
        self,
        booking_id: int,
        action: str,
        start_time: str | None = None,
        start_location: str | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> tuple[int, list[dict]]:
        action = "complete" if action == "completed" else action
        try:
            booking_row = self.db.execute(
                text(
                    """
                    SELECT booking_status
                    FROM hhome_collection_booking
                    WHERE id = :booking_id
                    FOR UPDATE
                    """
                ),
                {"booking_id": booking_id},
            ).fetchone()
            if not booking_row:
                raise ValueError("Booking not found")

            current_booking_status = int(booking_row.booking_status or 0)
            if current_booking_status in {3, 4, 5}:
                raise ValueError(
                    f"Booking is in terminal status {current_booking_status}. No further action allowed"
                )

            if action == "assign":
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking
                        SET booking_status = 1
                        WHERE id = :booking_id
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 1
                        WHERE booking_id = :booking_id
                          AND booking_patient_status = 0
                        """
                    ),
                    {"booking_id": booking_id},
                )
                final_status = 1

            elif action == "start":
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking
                        SET booking_status = 2
                        WHERE id = :booking_id
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 2
                        WHERE booking_id = :booking_id
                          AND booking_patient_status IN (0, 1)
                        """
                    ),
                    {"booking_id": booking_id},
                )
                final_status = 2

            elif action == "stop":
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking
                        SET booking_status = 1
                        WHERE id = :booking_id
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 1
                        WHERE booking_id = :booking_id
                          AND booking_patient_status = 2
                        """
                    ),
                    {"booking_id": booking_id},
                )
                final_status = 1

            elif action == "complete":
                if not self.booking_has_patients(booking_id):
                    raise ValueError("No patients found for this booking")
                status_counts_before = self.get_booking_patient_status_counts(booking_id)
                total_patients = sum(status_counts_before.values())
                all_cancelled = status_counts_before.get(4, 0) == total_patients and total_patients > 0
                if all_cancelled:
                    raise ValueError("All patients are cancelled. Use booking cancel.")

                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 3
                        WHERE booking_id = :booking_id
                          AND booking_patient_status IN (0, 1, 2)
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self._update_pending_tests_status(
                    booking_id=booking_id,
                    to_status=1,
                )
                status_counts_after = self.get_booking_patient_status_counts(booking_id)
                all_completed = (
                    status_counts_after.get(3, 0) == sum(status_counts_after.values())
                    and sum(status_counts_after.values()) > 0
                )
                has_completed = status_counts_after.get(3, 0) > 0
                has_cancelled = status_counts_after.get(4, 0) > 0

                if all_completed:
                    final_status = 3
                elif has_completed and has_cancelled:
                    final_status = 5
                else:
                    raise ValueError("Unable to compute final booking status for complete action")

                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking
                        SET booking_status = :final_status
                        WHERE id = :booking_id
                        """
                    ),
                    {"final_status": final_status, "booking_id": booking_id},
                )

            elif action == "cancel":
                status_counts = self.get_booking_patient_status_counts(booking_id)
                if status_counts.get(3, 0) > 0:
                    raise ValueError(
                        "Booking contains completed patients. Use booking complete for partial outcome."
                    )
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking
                        SET booking_status = 4
                        WHERE id = :booking_id
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 4
                        WHERE booking_id = :booking_id
                          AND booking_patient_status IN (0, 1, 2)
                        """
                    ),
                    {"booking_id": booking_id},
                )
                self._update_pending_tests_status(
                    booking_id=booking_id,
                    to_status=2,
                )
                final_status = 4
            else:
                raise ValueError(f"Unsupported action '{action}'")
            self._apply_booking_lifecycle_fields(
                booking_id=int(booking_id),
                action=action,
                start_time=start_time,
                start_location=start_location,
                complete_time=complete_time,
                complete_location=complete_location,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        patient_rows = self.get_booking_patient_status_rows(booking_id)
        return final_status, patient_rows

    def _insert_booking_action_audit(
        self,
        booking_id: int,
        action_type: str,
        reason_text: str,
        old_values: dict | None,
        new_values: dict | None,
        done_by: int,
    ) -> None:
        self.db.execute(
            text(
                """
                INSERT INTO hbooking_action_audit
                (booking_id, action_type, reason_text, old_values_json, new_values_json, done_by)
                VALUES (:booking_id, :action_type, :reason_text, :old_values_json, :new_values_json, :done_by)
                """
            ),
            {
                "booking_id": int(booking_id),
                "action_type": str(action_type or "").strip() or None,
                "reason_text": str(reason_text or "").strip() or None,
                "old_values_json": json.dumps(old_values, ensure_ascii=False) if old_values is not None else None,
                "new_values_json": json.dumps(new_values, ensure_ascii=False) if new_values is not None else None,
                "done_by": int(done_by or 0),
            },
        )

    def cancel_booking_with_lead(
        self,
        booking_id: int,
        actor_user_id: int,
        reason_text: str,
        remark: str | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
        reschedule_requested: bool = False,
        proposed_visit_date: str | None = None,
        proposed_time_slot: str | None = None,
    ) -> tuple[int, bool, str | None]:
        reason = str(reason_text or "").strip()
        if not reason:
            raise ValueError("Cancel reason is required")

        booking = self.db.execute(
            text(
                """
                SELECT id, booking_code, caller_id, booking_status, preferred_visit_date, preferred_time_slot
                FROM hhome_collection_booking
                WHERE id=:booking_id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().first()
        if not booking:
            raise ValueError("Booking not found")

        status_now = int(booking.get("booking_status") or 0)
        if status_now in {3, 4}:
            raise ValueError("Completed/Cancelled booking cannot be cancelled")

        self.db.execute(
            text("UPDATE hhome_collection_booking SET booking_status=4 WHERE id=:booking_id"),
            {"booking_id": int(booking_id)},
        )
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_patient
                SET booking_patient_status=4
                WHERE booking_id=:booking_id
                  AND booking_patient_status IN (0,1,2)
                """
            ),
            {"booking_id": int(booking_id)},
        )
        self._update_pending_tests_status(
            booking_id=int(booking_id),
            to_status=2,
        )
        self._apply_booking_lifecycle_fields(
            booking_id=int(booking_id),
            action="cancel",
            complete_time=complete_time,
            complete_location=complete_location,
        )

        lead_created = False
        lead_id = None

        lead_meta = self.db.execute(
            text(
                """
                SELECT cm.primary_mobile,
                       GROUP_CONCAT(DISTINCT TRIM(CONCAT_WS(' ', p.title, p.full_name)) ORDER BY p.full_name SEPARATOR ', ') AS patient_names,
                       COUNT(DISTINCT bp.patient_id) AS patient_count
                FROM hhome_collection_booking b
                INNER JOIN hcaller_master cm ON cm.id=b.caller_id
                LEFT JOIN hhome_collection_booking_patient bp ON bp.booking_id=b.id
                LEFT JOIN hpatient_master p ON p.id=bp.patient_id
                WHERE b.id=:booking_id
                GROUP BY cm.primary_mobile
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().first() or {}

        mobile = str(lead_meta.get("primary_mobile") or "").strip()
        patient_names = str(lead_meta.get("patient_names") or "").strip()
        patient_count = int(lead_meta.get("patient_count") or 0)
        remark_txt = str(remark or "").strip()

        res_note = "Reschedule not requested."
        if bool(reschedule_requested):
            if (proposed_visit_date or "").strip() and (proposed_time_slot or "").strip():
                res_note = f"Reschedule requested for {str(proposed_visit_date).strip()} {str(proposed_time_slot).strip()}."
            else:
                res_note = "Reschedule requested; date/slot to be finalized."

        lead_summary = f"This was a Home Collection booking cancelled due to {reason}. {res_note}"
        if remark_txt:
            lead_summary = f"{lead_summary} Remark: {remark_txt}"

        if bool(reschedule_requested) and mobile:
            self.db.execute(
                text(
                    """
                    INSERT INTO leads
                    (phone, wa_only, name, alt_phone, alt_wa_only, visit_window, prescription, remarks, tags, num_patients, created_by, status)
                    VALUES (:phone, 0, :name, '', 0, 'Flexible', '', :remarks, 'home_collection_cancel', :num_patients, :created_by, 'Open')
                    """
                ),
                {
                    "phone": mobile,
                    "name": patient_names or "Home Collection Cancellation",
                    "remarks": lead_summary,
                    "num_patients": max(1, patient_count),
                    "created_by": str(actor_user_id),
                },
            )
            new_id_row = self.db.execute(text("SELECT LAST_INSERT_ID() AS lid")).mappings().first() or {}
            new_pk = int(new_id_row.get("lid") or 0)
            if new_pk > 0:
                lead_id = f"LD-{new_pk:03d}"
                self.db.execute(
                    text("UPDATE leads SET lead_id=:lead_id WHERE id=:id"),
                    {"lead_id": lead_id, "id": new_pk},
                )
                lead_created = True

        self._insert_booking_action_audit(
            booking_id=int(booking_id),
            action_type="CANCEL",
            reason_text=(f"{reason} | {remark_txt}" if remark_txt else reason),
            old_values={
                "preferred_visit_date": str(booking.get("preferred_visit_date") or ""),
                "preferred_time_slot": str(booking.get("preferred_time_slot") or "").strip(),
                "booking_status": status_now,
            },
            new_values={
                "booking_status": 4,
                "reschedule_requested": bool(reschedule_requested),
                "proposed_visit_date": str(proposed_visit_date or "").strip() or None,
                "proposed_time_slot": str(proposed_time_slot or "").strip() or None,
                "lead_created": lead_created,
                "lead_id": lead_id,
            },
            done_by=int(actor_user_id),
        )

        self.db.commit()
        return 4, lead_created, lead_id

    def apply_appointment_action(
        self,
        booking_id: int,
        appointment_id: int,
        user_id: int,
        action: str,
        start_time: str | None = None,
        start_location: str | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> tuple[int, list[dict], str]:
        action = "complete" if action == "completed" else action
        cols = self._get_appointment_columns()
        if not cols:
            raise ValueError("Appointment table not available")
        id_col = "id" if "id" in cols else "appointment_id"
        if "appointment_status" not in cols:
            raise ValueError("Appointment status column not available")

        where_user = ""
        params: dict[str, object] = {
            "booking_id": booking_id,
            "appointment_id": appointment_id,
        }
        if "assigned_phlebotomist_id" in cols:
            where_user = " AND assigned_phlebotomist_id = :user_id"
            params["user_id"] = user_id

        row = self.db.execute(
            text(
                f"""
                SELECT booking_id, appointment_status
                FROM hhome_collection_booking_appointment
                WHERE {id_col} = :appointment_id
                  AND booking_id = :booking_id
                  {where_user}
                LIMIT 1
                FOR UPDATE
                """
            ),
            params,
        ).fetchone()
        if not row:
            raise ValueError("Appointment not found or not assigned to current user")

        current_status = self._normalize_status_code(row.appointment_status) or 0
        if current_status in {3, 4}:
            raise ValueError(f"Appointment is in terminal status {current_status}")

        selected_booking_id, _appointment_status, selected_ids, _patient_scope = (
            self.get_appointment_selected_patient_ids(
                appointment_id=appointment_id,
                user_id=user_id,
            )
        )
        if selected_booking_id is not None and int(selected_booking_id) != int(booking_id):
            raise ValueError("Appointment does not belong to provided booking")

        action_to_status = {
            "assign": 1,
            "start": 2,
            "stop": 1,
            "complete": 3,
            "cancel": 4,
        }
        if action not in action_to_status:
            raise ValueError(f"Unsupported action '{action}'")
        final_status = action_to_status[action]

        try:
            patient_params: dict[str, object] = {"booking_id": booking_id}
            scope_sql = self._build_patient_scope_where(
                patient_ids=selected_ids if selected_ids else None,
                params=patient_params,
                column_name="patient_id",
            )

            self.db.execute(
                text(
                    f"""
                    UPDATE hhome_collection_booking_appointment
                    SET appointment_status = :final_status
                    WHERE {id_col} = :appointment_id
                      AND booking_id = :booking_id
                    """
                ),
                {
                    "final_status": final_status,
                    "appointment_id": appointment_id,
                    "booking_id": booking_id,
                },
            )
            self._apply_appointment_lifecycle_fields(
                appointment_id=int(appointment_id),
                booking_id=int(booking_id),
                action=action,
                start_time=start_time,
                start_location=start_location,
                complete_time=complete_time,
                complete_location=complete_location,
            )

            if action == "complete":
                self._update_pending_tests_status(
                    booking_id=booking_id,
                    to_status=1,
                    patient_ids=selected_ids if selected_ids else None,
                )
            elif action == "cancel":
                self._update_pending_tests_status(
                    booking_id=booking_id,
                    to_status=2,
                    patient_ids=selected_ids if selected_ids else None,
                )

            self._save_appointment_patient_context(
                booking_id=booking_id,
                appointment_id=appointment_id,
                patient_ids=selected_ids if selected_ids else None,
                status_value=final_status,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        selected_booking_id, _appointment_status, selected_ids, patient_scope = (
            self.get_appointment_selected_patient_ids(
                appointment_id=appointment_id,
                user_id=user_id,
            )
        )
        if selected_booking_id is None or int(selected_booking_id) != int(booking_id):
            patient_scope = "BOOKING_ALL_FALLBACK"
            selected_ids = []

        patient_rows = self.get_booking_patient_status_rows_filtered(
            booking_id=booking_id,
            patient_ids=selected_ids if selected_ids else None,
        )
        return final_status, patient_rows, patient_scope

    def cancel_single_booking_patient(
        self,
        booking_id: int,
        booking_patient_id: int,
    ) -> list[dict]:
        try:
            row = self.db.execute(
                text(
                    """
                    SELECT patient_id, booking_patient_status
                    FROM hhome_collection_booking_patient
                    WHERE id = :booking_patient_id AND booking_id = :booking_id
                    FOR UPDATE
                    """
                ),
                {
                    "booking_patient_id": booking_patient_id,
                    "booking_id": booking_id,
                },
            ).fetchone()
            if not row:
                raise ValueError("Booking patient row not found")

            current_status = int(row.booking_patient_status or 0)
            if current_status == 3:
                raise ValueError("Completed patient cannot be cancelled")
            if current_status != 4:
                patient_id = int(row.patient_id)
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET booking_patient_status = 4
                        WHERE id = :booking_patient_id AND booking_id = :booking_id
                        """
                    ),
                    {
                        "booking_patient_id": booking_patient_id,
                        "booking_id": booking_id,
                    },
                )
                self._update_pending_tests_status(
                    booking_id=booking_id,
                    to_status=2,
                    patient_ids=[patient_id],
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return self.get_booking_patient_status_rows(booking_id)

    def get_booking_patient_context(self, booking_id: int, patient_id: int) -> dict | None:
        row = self.db.execute(
            text(
                """
                SELECT
                    bp.id AS booking_patient_id,
                    bp.booking_id,
                    bp.patient_id,
                    b.caller_id,
                    b.booking_status,
                    p.patient_code,
                    p.contact_mobile,
                    p.alternate_mobile
                FROM hhome_collection_booking_patient bp
                INNER JOIN hhome_collection_booking b ON b.id = bp.booking_id
                INNER JOIN hpatient_master p ON p.id = bp.patient_id
                WHERE bp.booking_id = :booking_id AND bp.patient_id = :patient_id
                LIMIT 1
                """
            ),
            {"booking_id": booking_id, "patient_id": patient_id},
        ).fetchone()
        if not row:
            return None
        return dict(row._mapping)

    def get_patient_id_by_booking_patient_id(
        self, booking_id: int, booking_patient_id: int
    ) -> int | None:
        row = self.db.execute(
            text(
                """
                SELECT patient_id
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id AND id = :booking_patient_id
                LIMIT 1
                """
            ),
            {
                "booking_id": booking_id,
                "booking_patient_id": booking_patient_id,
            },
        ).fetchone()
        if not row:
            return None
        return int(row.patient_id)

    def edit_patient_in_booking(
        self,
        booking_id: int,
        patient_id: int,
        caller_id: int,
        actor_user_id: int,
        update_fields: dict,
        old_primary_mobile_norm: str | None,
        new_primary_mobile_norm: str | None,
        old_alternate_mobile_norm: str | None,
        new_alternate_mobile_norm: str | None,
        primary_mobile_raw: str | None,
        alternate_mobile_raw: str | None,
    ) -> dict:
        linked_mobiles: list[str] = []
        try:
            set_clauses: list[str] = []
            params: dict = {"patient_id": patient_id, "updated_by": actor_user_id}
            field_map = {
                "title": "title",
                "full_name": "full_name",
                "gender": "gender",
                "date_of_birth": "date_of_birth",
                "age_years": "age_years",
                "primary_mobile": "contact_mobile",
                "alternate_mobile": "alternate_mobile",
                "labmate_pid": "labmate_pid",
                "panel_company": "panel_company",
                "tag": "tag",
            }
            for src_key, col_name in field_map.items():
                if src_key in update_fields:
                    set_clauses.append(f"{col_name} = :{src_key}")
                    params[src_key] = update_fields[src_key]

            if set_clauses:
                set_clauses.append("updated_by = :updated_by")
                self.db.execute(
                    text(
                        f"""
                        UPDATE hpatient_master
                        SET {", ".join(set_clauses)}
                        WHERE id = :patient_id
                        """
                    ),
                    params,
                )

            def update_mobile_map_same_row(
                old_norm: str | None,
                new_norm: str | None,
                new_raw: str | None,
                phone_type: str,
            ) -> None:
                if not new_norm:
                    return
                conflict = self.db.execute(
                    text(
                        """
                        SELECT id, caller_id
                        FROM hcaller_mobile_map
                        WHERE mobile_norm = :mobile_norm AND is_active = 1
                        LIMIT 1
                        """
                    ),
                    {"mobile_norm": new_norm},
                ).fetchone()
                if conflict and int(conflict.caller_id) != caller_id:
                    raise ValueError(
                        f"Mobile {new_norm} is already mapped to another caller ({int(conflict.caller_id)})"
                    )

                if old_norm and old_norm != new_norm:
                    old_row = self.db.execute(
                        text(
                            """
                            SELECT id
                            FROM hcaller_mobile_map
                            WHERE caller_id = :caller_id
                              AND mobile_norm = :old_norm
                              AND is_active = 1
                            LIMIT 1
                            """
                        ),
                        {"caller_id": caller_id, "old_norm": old_norm},
                    ).fetchone()
                    if old_row:
                        self.db.execute(
                            text(
                                """
                                UPDATE hcaller_mobile_map
                                SET mobile_norm = :new_norm,
                                    mobile_raw = :new_raw,
                                    phone_type = :phone_type,
                                    is_active = 1
                                WHERE id = :id
                                """
                            ),
                            {
                                "new_norm": new_norm,
                                "new_raw": new_raw or new_norm,
                                "phone_type": phone_type,
                                "id": int(old_row.id),
                            },
                        )
                        if new_norm not in linked_mobiles:
                            linked_mobiles.append(new_norm)
                        return

                same_row = self.db.execute(
                    text(
                        """
                        SELECT id
                        FROM hcaller_mobile_map
                        WHERE caller_id = :caller_id
                          AND mobile_norm = :mobile_norm
                        LIMIT 1
                        """
                    ),
                    {"caller_id": caller_id, "mobile_norm": new_norm},
                ).fetchone()
                if same_row:
                    self.db.execute(
                        text(
                            """
                            UPDATE hcaller_mobile_map
                            SET mobile_raw = :mobile_raw,
                                phone_type = :phone_type,
                                is_active = 1
                            WHERE id = :id
                            """
                        ),
                        {
                            "mobile_raw": new_raw or new_norm,
                            "phone_type": phone_type,
                            "id": int(same_row.id),
                        },
                    )
                else:
                    self.db.execute(
                        text(
                            """
                            INSERT INTO hcaller_mobile_map
                            (caller_id, mobile_norm, mobile_raw, phone_type, is_active, created_by)
                            VALUES
                            (:caller_id, :mobile_norm, :mobile_raw, :phone_type, 1, :created_by)
                            """
                        ),
                        {
                            "caller_id": caller_id,
                            "mobile_norm": new_norm,
                            "mobile_raw": new_raw or new_norm,
                            "phone_type": phone_type,
                            "created_by": actor_user_id,
                        },
                    )
                if new_norm not in linked_mobiles:
                    linked_mobiles.append(new_norm)

            if "primary_mobile" in update_fields:
                update_mobile_map_same_row(
                    old_norm=old_primary_mobile_norm,
                    new_norm=new_primary_mobile_norm,
                    new_raw=primary_mobile_raw,
                    phone_type="Patient",
                )
            if "alternate_mobile" in update_fields:
                update_mobile_map_same_row(
                    old_norm=old_alternate_mobile_norm,
                    new_norm=new_alternate_mobile_norm,
                    new_raw=alternate_mobile_raw,
                    phone_type="PatientAlt",
                )

            self.db.commit()
            return {
                "booking_id": booking_id,
                "patient_id": patient_id,
                "linked_mobiles": linked_mobiles,
                "message": "Patient updated successfully",
            }
        except Exception:
            self.db.rollback()
            raise

    def link_existing_patient_to_booking_same_address(
        self,
        booking_id: int,
        caller_id: int,
        patient_id: int,
        address_id: int,
        booking_status: int,
        actor_user_id: int,
        auto_commit: bool = True,
    ) -> dict:
        try:
            patient_row = self.db.execute(
                text(
                    """
                    SELECT id, patient_code
                    FROM hpatient_master
                    WHERE id = :patient_id
                    LIMIT 1
                    """
                ),
                {"patient_id": patient_id},
            ).fetchone()
            if not patient_row:
                raise ValueError(f"Patient {patient_id} not found")

            caller_link_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hcaller_patient_link
                    WHERE caller_id = :caller_id AND patient_id = :patient_id
                    LIMIT 1
                    """
                ),
                {"caller_id": caller_id, "patient_id": patient_id},
            ).fetchone()
            if not caller_link_exists:
                self.db.execute(
                    text(
                        """
                        INSERT INTO hcaller_patient_link (caller_id, patient_id, is_active, created_by)
                        VALUES (:caller_id, :patient_id, 1, :created_by)
                        """
                    ),
                    {
                        "caller_id": caller_id,
                        "patient_id": patient_id,
                        "created_by": actor_user_id,
                    },
                )

            address_link_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hpatient_address_link
                    WHERE patient_id = :patient_id AND address_id = :address_id
                    LIMIT 1
                    """
                ),
                {"patient_id": patient_id, "address_id": address_id},
            ).fetchone()
            if not address_link_exists:
                self.db.execute(
                    text(
                        """
                        INSERT INTO hpatient_address_link (patient_id, address_id, is_default, is_active, created_by)
                        VALUES (:patient_id, :address_id, 0, 1, :created_by)
                        """
                    ),
                    {
                        "patient_id": patient_id,
                        "address_id": address_id,
                        "created_by": actor_user_id,
                    },
                )

            booking_patient_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hhome_collection_booking_patient
                    WHERE booking_id = :booking_id AND patient_id = :patient_id
                    LIMIT 1
                    """
                ),
                {"booking_id": booking_id, "patient_id": patient_id},
            ).fetchone()
            if booking_patient_exists:
                booking_patient_id = int(booking_patient_exists.id)
                if auto_commit:
                    self.db.commit()
                return {
                    "booking_id": booking_id,
                    "patient_id": patient_id,
                    "patient_code": str(patient_row.patient_code or ""),
                    "booking_patient_id": booking_patient_id,
                    "linked_to_booking": True,
                    "linked_mobiles": [],
                    "message": "Patient already linked to booking",
                }

            booking_patient_insert = self.db.execute(
                text(
                    """
                    INSERT INTO hhome_collection_booking_patient
                    (booking_id, patient_id, booking_patient_status, created_by)
                    VALUES (:booking_id, :patient_id, :booking_patient_status, :created_by)
                    """
                ),
                {
                    "booking_id": booking_id,
                    "patient_id": patient_id,
                    "booking_patient_status": 2 if int(booking_status) == 2 else 1,
                    "created_by": actor_user_id,
                },
            )
            booking_patient_id = int(booking_patient_insert.lastrowid)
            if auto_commit:
                self.db.commit()
            return {
                "booking_id": booking_id,
                "patient_id": patient_id,
                "patient_code": str(patient_row.patient_code or ""),
                "booking_patient_id": booking_patient_id,
                "linked_to_booking": True,
                "linked_mobiles": [],
                "message": "Existing patient linked to booking",
            }
        except Exception:
            self.db.rollback()
            raise

    def add_patient_to_booking_same_address(
        self,
        booking_id: int,
        caller_id: int,
        address_id: int,
        booking_status: int,
        payload: dict,
        actor_user_id: int,
        auto_commit: bool = True,
    ) -> dict:
        temp_code = f"TMP-{uuid4().hex[:12].upper()}"
        linked_mobiles: list[str] = []
        try:
            patient_insert = self.db.execute(
                text(
                    """
                    INSERT INTO hpatient_master
                    (
                        patient_code, title, full_name, labmate_pid, panel_company, tag,
                        gender, date_of_birth, age_years, contact_mobile, alternate_mobile,
                        patient_status, created_by, updated_by
                    )
                    VALUES
                    (
                        :patient_code, :title, :full_name, :labmate_pid, :panel_company, :tag,
                        :gender, :date_of_birth, :age_years, :contact_mobile, :alternate_mobile,
                        'Active', :created_by, :updated_by
                    )
                    """
                ),
                {
                    "patient_code": temp_code,
                    "title": payload.get("title"),
                    "full_name": payload["full_name"],
                    "labmate_pid": payload.get("labmate_pid"),
                    "panel_company": payload.get("panel_company"),
                    "tag": payload.get("tag"),
                    "gender": payload["gender"],
                    "date_of_birth": payload.get("date_of_birth"),
                    "age_years": payload.get("age_years"),
                    "contact_mobile": payload["primary_mobile"],
                    "alternate_mobile": payload.get("alternate_mobile"),
                    "created_by": actor_user_id,
                    "updated_by": actor_user_id,
                },
            )
            patient_id = int(patient_insert.lastrowid)
            patient_code = f"HPT-HC-{patient_id:06d}"
            self.db.execute(
                text("UPDATE hpatient_master SET patient_code = :patient_code WHERE id = :patient_id"),
                {"patient_code": patient_code, "patient_id": patient_id},
            )

            caller_link_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hcaller_patient_link
                    WHERE caller_id = :caller_id AND patient_id = :patient_id
                    LIMIT 1
                    """
                ),
                {"caller_id": caller_id, "patient_id": patient_id},
            ).fetchone()
            if not caller_link_exists:
                self.db.execute(
                    text(
                        """
                        INSERT INTO hcaller_patient_link (caller_id, patient_id, is_active, created_by)
                        VALUES (:caller_id, :patient_id, 1, :created_by)
                        """
                    ),
                    {
                        "caller_id": caller_id,
                        "patient_id": patient_id,
                        "created_by": actor_user_id,
                    },
                )

            def upsert_caller_mobile(mobile_norm: str, mobile_raw: str, phone_type: str) -> None:
                conflict = self.db.execute(
                    text(
                        """
                        SELECT id, caller_id
                        FROM hcaller_mobile_map
                        WHERE mobile_norm = :mobile_norm AND is_active = 1
                        LIMIT 1
                        """
                    ),
                    {"mobile_norm": mobile_norm},
                ).fetchone()
                if conflict and int(conflict.caller_id) != caller_id:
                    raise ValueError(
                        f"Mobile {mobile_norm} is already mapped to another caller ({int(conflict.caller_id)})"
                    )
                if conflict and int(conflict.caller_id) == caller_id:
                    self.db.execute(
                        text(
                            """
                            UPDATE hcaller_mobile_map
                            SET mobile_raw = :mobile_raw, phone_type = :phone_type, is_active = 1
                            WHERE id = :id
                            """
                        ),
                        {
                            "mobile_raw": mobile_raw,
                            "phone_type": phone_type,
                            "id": int(conflict.id),
                        },
                    )
                else:
                    self.db.execute(
                        text(
                            """
                            INSERT INTO hcaller_mobile_map
                            (caller_id, mobile_norm, mobile_raw, phone_type, is_active, created_by)
                            VALUES
                            (:caller_id, :mobile_norm, :mobile_raw, :phone_type, 1, :created_by)
                            """
                        ),
                        {
                            "caller_id": caller_id,
                            "mobile_norm": mobile_norm,
                            "mobile_raw": mobile_raw,
                            "phone_type": phone_type,
                            "created_by": actor_user_id,
                        },
                    )
                if mobile_norm not in linked_mobiles:
                    linked_mobiles.append(mobile_norm)

            upsert_caller_mobile(
                mobile_norm=payload["primary_mobile_norm"],
                mobile_raw=payload["primary_mobile_raw"],
                phone_type="Patient",
            )
            if payload.get("alternate_mobile_norm"):
                upsert_caller_mobile(
                    mobile_norm=payload["alternate_mobile_norm"],
                    mobile_raw=payload.get("alternate_mobile_raw") or payload["alternate_mobile_norm"],
                    phone_type="PatientAlt",
                )

            address_link_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hpatient_address_link
                    WHERE patient_id = :patient_id AND address_id = :address_id
                    LIMIT 1
                    """
                ),
                {"patient_id": patient_id, "address_id": address_id},
            ).fetchone()
            if not address_link_exists:
                self.db.execute(
                    text(
                        """
                        INSERT INTO hpatient_address_link (patient_id, address_id, is_default, is_active, created_by)
                        VALUES (:patient_id, :address_id, 0, 1, :created_by)
                        """
                    ),
                    {
                        "patient_id": patient_id,
                        "address_id": address_id,
                        "created_by": actor_user_id,
                    },
                )

            booking_patient_exists = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hhome_collection_booking_patient
                    WHERE booking_id = :booking_id AND patient_id = :patient_id
                    LIMIT 1
                    """
                ),
                {"booking_id": booking_id, "patient_id": patient_id},
            ).fetchone()
            if booking_patient_exists:
                booking_patient_id = int(booking_patient_exists.id)
                if auto_commit:
                    self.db.commit()
                return {
                    "booking_id": booking_id,
                    "patient_id": patient_id,
                    "patient_code": patient_code,
                    "booking_patient_id": booking_patient_id,
                    "linked_to_booking": True,
                    "linked_mobiles": linked_mobiles,
                    "message": "Patient already linked to booking",
                }

            booking_patient_insert = self.db.execute(
                text(
                    """
                    INSERT INTO hhome_collection_booking_patient
                    (booking_id, patient_id, booking_patient_status, created_by)
                    VALUES (:booking_id, :patient_id, :booking_patient_status, :created_by)
                    """
                ),
                {
                    "booking_id": booking_id,
                    "patient_id": patient_id,
                    "booking_patient_status": 2 if int(booking_status) == 2 else 1,
                    "created_by": actor_user_id,
                },
            )
            booking_patient_id = int(booking_patient_insert.lastrowid)

            if auto_commit:
                self.db.commit()
            return {
                "booking_id": booking_id,
                "patient_id": patient_id,
                "patient_code": patient_code,
                "booking_patient_id": booking_patient_id,
                "linked_to_booking": True,
                "linked_mobiles": linked_mobiles,
                "message": "Patient added to booking with same address",
            }
        except Exception:
            self.db.rollback()
            raise

    def get_patient_document_paths(self, patient_id: int) -> list[str]:
        row = self.db.execute(
            text(
                """
                SELECT patient_documents
                FROM hpatient_master
                WHERE id = :patient_id
                LIMIT 1
                """
            ),
            {"patient_id": patient_id},
        ).fetchone()
        if not row or row.patient_documents is None:
            return []
        raw = str(row.patient_documents).strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def update_patient_documents(self, patient_id: int, documents: list[str]) -> None:
        csv_value = ",".join(documents)
        self.db.execute(
            text(
                """
                UPDATE hpatient_master
                SET patient_documents = :patient_documents
                WHERE id = :patient_id
                """
            ),
            {"patient_documents": csv_value, "patient_id": patient_id},
        )

    def get_patient_prescription_paths(self, booking_id: int, patient_id: int) -> list[str]:
        row = self.db.execute(
            text(
                """
                SELECT prescription_files
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id AND patient_id = :patient_id
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"booking_id": int(booking_id), "patient_id": int(patient_id)},
        ).fetchone()
        if not row or row.prescription_files is None:
            return []
        raw = str(row.prescription_files).strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def update_patient_prescription_files(self, booking_id: int, patient_id: int, files: list[str]) -> None:
        csv_value = ",".join(files)
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_patient
                SET prescription_files = :prescription_files
                WHERE booking_id = :booking_id AND patient_id = :patient_id
                """
            ),
            {"prescription_files": csv_value, "booking_id": int(booking_id), "patient_id": int(patient_id)},
        )

    def get_patient_photo_paths(self, booking_id: int, patient_id: int) -> list[str]:
        cols = self._get_table_columns("hhome_collection_booking_patient")
        if "patient_photo_files" not in cols:
            return []
        row = self.db.execute(
            text(
                """
                SELECT patient_photo_files
                FROM hhome_collection_booking_patient
                WHERE booking_id = :booking_id AND patient_id = :patient_id
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"booking_id": int(booking_id), "patient_id": int(patient_id)},
        ).fetchone()
        if not row or row.patient_photo_files is None:
            return []
        raw = str(row.patient_photo_files).strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def update_patient_photo_files(self, booking_id: int, patient_id: int, files: list[str]) -> None:
        cols = self._get_table_columns("hhome_collection_booking_patient")
        if "patient_photo_files" not in cols:
            return
        csv_value = ",".join(files)
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_patient
                SET patient_photo_files = :patient_photo_files
                WHERE booking_id = :booking_id AND patient_id = :patient_id
                """
            ),
            {"patient_photo_files": csv_value or None, "booking_id": int(booking_id), "patient_id": int(patient_id)},
        )


    def save_booking_tests_and_amounts(
        self,
        booking_id: int,
        actor_user_id: int,
        desired_rows: list[dict],
        patient_panel_map: dict[int, dict] | None,
        subtotal: float,
        final_discount: float,
        additional_discount: float,
        final_amount: float,
        credit_amount: float = 0.0,
        paying_amount: float = 0.0,
    ) -> tuple[int, int]:
        active_expr = "CASE WHEN test_status IS NULL OR TRIM(test_status)='' THEN 0 WHEN UPPER(TRIM(test_status)) IN ('PENDING','0') THEN 0 WHEN UPPER(TRIM(test_status)) IN ('COMPLETED','1') THEN 1 WHEN UPPER(TRIM(test_status)) IN ('DROPPED','CANCELLED','2') THEN 2 ELSE 0 END"
        bp_rows = self.db.execute(
            text("SELECT id, patient_id FROM hhome_collection_booking_patient WHERE booking_id=:booking_id"),
            {"booking_id": booking_id},
        ).mappings().all()
        bp_map = {int(r["patient_id"]): int(r["id"]) for r in bp_rows}
        panel_cols = self._get_table_columns("hhome_collection_booking_patient")
        if patient_panel_map and {"selected_comp_cat_ids", "selected_charge_modes", "selected_panel_companies"}.issubset(panel_cols):
            for patient_id, meta in (patient_panel_map or {}).items():
                if int(patient_id) not in bp_map:
                    continue
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET selected_comp_cat_ids=:selected_comp_cat_ids,
                            selected_charge_modes=:selected_charge_modes,
                            selected_panel_companies=:selected_panel_companies
                        WHERE booking_id=:booking_id AND patient_id=:patient_id
                        """
                    ),
                    {
                        "selected_comp_cat_ids": (meta or {}).get("selected_comp_cat_ids"),
                        "selected_charge_modes": (meta or {}).get("selected_charge_modes"),
                        "selected_panel_companies": (meta or {}).get("selected_panel_companies"),
                        "booking_id": booking_id,
                        "patient_id": int(patient_id),
                    },
                )

        existing_active = self.db.execute(
            text(f"SELECT id, patient_id, booked_code FROM hhome_collection_booking_patient_test WHERE booking_id=:booking_id AND {active_expr}=0"),
            {"booking_id": booking_id},
        ).mappings().all()
        existing_keys = {(int(r["patient_id"]), str(r["booked_code"]).strip().upper()): int(r["id"]) for r in existing_active if r.get("booked_code")}
        desired_keys = {(int(r["patient_id"]), str(r["booked_code"]).strip().upper()) for r in desired_rows if r.get("booked_code")}

        to_drop = [rid for key, rid in existing_keys.items() if key not in desired_keys]
        dropped_count = 0
        if to_drop:
            self.db.execute(
                text("UPDATE hhome_collection_booking_patient_test SET test_status='2', dropped_at=NOW(), dropped_by=:uid WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
                {"uid": actor_user_id, "ids": to_drop},
            )
            dropped_count = len(to_drop)

        active_count = 0
        for row in desired_rows:
            patient_id = int(row["patient_id"])
            bp_id = bp_map.get(patient_id)
            if not bp_id:
                continue
            code = str(row.get("booked_code") or "").strip().upper()
            if not code:
                continue
            found = self.db.execute(
                text("SELECT id FROM hhome_collection_booking_patient_test WHERE booking_id=:booking_id AND patient_id=:patient_id AND booked_code=:booked_code ORDER BY id DESC LIMIT 1"),
                {"booking_id": booking_id, "patient_id": patient_id, "booked_code": code},
            ).fetchone()
            params = {
                "booking_id": booking_id,
                "booking_patient_id": bp_id,
                "patient_id": patient_id,
                "comp_cat_id": row.get("comp_cat_id"),
                "booked_code": code,
                "test_name": row.get("test_name"),
                "charge": float(row.get("charge") or 0),
                "mrp": float(row.get("mrp") or 0),
                "max_discount": float(row.get("max_discount") or 0),
                "uid": actor_user_id,
            }
            if found:
                self.db.execute(
                    text("UPDATE hhome_collection_booking_patient_test SET booking_patient_id=:booking_patient_id, comp_cat_id=:comp_cat_id, test_name=:test_name, charge=:charge, mrp=:mrp, max_discount=:max_discount, test_status='0', dropped_at=NULL, dropped_by=NULL WHERE id=:id"),
                    {**params, "id": int(found[0])},
                )
            else:
                self.db.execute(
                    text("INSERT INTO hhome_collection_booking_patient_test (booking_id, booking_patient_id, patient_id, comp_cat_id, booked_code, test_name, charge, mrp, max_discount, test_status, created_by) VALUES (:booking_id, :booking_patient_id, :patient_id, :comp_cat_id, :booked_code, :test_name, :charge, :mrp, :max_discount, '0', :uid)"),
                    params,
                )
            active_count += 1

        self.db.execute(
            text("UPDATE hhome_collection_booking SET F_Apt_Am=:sub_total, F_dis=:f_dis, Ad_Dis=:ad_dis, total_amount=:total_amount WHERE id=:booking_id"),
            {
                "sub_total": round(float(subtotal or 0), 2),
                "f_dis": round(float(final_discount or 0), 2),
                "ad_dis": round(float(additional_discount or 0), 2),
                "total_amount": round(float(final_amount or 0), 2),
                "booking_id": booking_id,
            },
        )
        self._recompute_booking_amounts_from_active_tests(int(booking_id))

        self.db.commit()
        return active_count, dropped_count

    def replace_pending_child_tests_for_booking(
        self,
        booking_id: int,
        pending_rows: list[dict],
        source_type: str = "BOOKING",
        appointment_id: int | None = None,
        actor_user_id: int | None = None,
        patient_ids_scope: list[int] | None = None,
    ) -> None:
        """
        Status-based pending child persistence.
        - No hard delete.
        - Previous open rows for same scope are marked resolved (pending_status=1).
        - Current payload rows are inserted as open (pending_status=0).
        """
        src = str(source_type or "BOOKING").strip().upper() or "BOOKING"
        appt_id = int(appointment_id or 0) if src == "APPOINTMENT" else None

        scope_ids = sorted({int(x) for x in (patient_ids_scope or []) if int(x) > 0})

        if src == "APPOINTMENT" and appt_id:
            self.db.execute(
                text(
                    """
                    UPDATE hcb_patient_test_pendingchildtest
                    SET pending_status = 1,
                        resolved_at = NOW(),
                        resolved_by = :resolved_by,
                        updated_at = NOW()
                    WHERE booking_id = :booking_id
                      AND UPPER(TRIM(COALESCE(source_type, 'BOOKING'))) = 'APPOINTMENT'
                      AND IFNULL(appointment_id, 0) = :appointment_id
                      AND IFNULL(pending_status, 0) = 0
                    """
                ),
                {
                    "booking_id": int(booking_id),
                    "appointment_id": int(appt_id),
                    "resolved_by": self._to_int(actor_user_id),
                },
            )
            if scope_ids:
                self.db.execute(
                    text(
                        """
                        UPDATE hcb_patient_test_pendingchildtest
                        SET pending_status = 1,
                            resolved_at = NOW(),
                            resolved_by = :resolved_by,
                            updated_at = NOW()
                        WHERE booking_id = :booking_id
                          AND UPPER(TRIM(COALESCE(source_type, 'BOOKING'))) = 'BOOKING'
                          AND patient_id IN :pids
                          AND IFNULL(pending_status, 0) = 0
                        """
                    ).bindparams(bindparam("pids", expanding=True)),
                    {
                        "booking_id": int(booking_id),
                        "resolved_by": self._to_int(actor_user_id),
                        "pids": scope_ids,
                    },
                )
            else:
                self.db.execute(
                    text(
                        """
                        UPDATE hcb_patient_test_pendingchildtest
                        SET pending_status = 1,
                            resolved_at = NOW(),
                            resolved_by = :resolved_by,
                            updated_at = NOW()
                        WHERE booking_id = :booking_id
                          AND UPPER(TRIM(COALESCE(source_type, 'BOOKING'))) = 'BOOKING'
                          AND IFNULL(pending_status, 0) = 0
                        """
                    ),
                    {
                        "booking_id": int(booking_id),
                        "resolved_by": self._to_int(actor_user_id),
                    },
                )
        else:
            self.db.execute(
                text(
                    """
                    UPDATE hcb_patient_test_pendingchildtest
                    SET pending_status = 1,
                        resolved_at = NOW(),
                        resolved_by = :resolved_by,
                        updated_at = NOW()
                    WHERE booking_id = :booking_id
                      AND UPPER(TRIM(COALESCE(source_type, 'BOOKING'))) = 'BOOKING'
                      AND IFNULL(pending_status, 0) = 0
                    """
                ),
                {
                    "booking_id": int(booking_id),
                    "resolved_by": self._to_int(actor_user_id),
                },
            )

        grouped: dict[tuple[int, int, int, str], dict] = {}

        for raw in (pending_rows or []):
            row = raw or {}
            try:
                row_booking_id = int(row.get("booking_id") or booking_id)
            except Exception:
                row_booking_id = int(booking_id)
            if row_booking_id != int(booking_id):
                continue

            booking_patient_id = self._to_int(row.get("booking_patient_id"))
            patient_id = self._to_int(row.get("patient_id"))
            root_booked_code = str(row.get("root_booked_code") or "").strip().upper() or None
            root_test_name = str(row.get("root_test_name") or "").strip() or None
            tube_name = str(row.get("tube_name") or row.get("tube") or "").strip() or None
            pending_items = row.get("pending") or row.get("pending_child_tests") or []

            if not patient_id or not root_booked_code:
                continue
            if not booking_patient_id:
                bp_row = self.db.execute(
                    text(
                        """
                        SELECT id
                        FROM hhome_collection_booking_patient
                        WHERE booking_id = :booking_id
                          AND patient_id = :patient_id
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "booking_id": int(booking_id),
                        "patient_id": int(patient_id),
                    },
                ).fetchone()
                if bp_row:
                    booking_patient_id = self._to_int(bp_row[0])
            if not booking_patient_id:
                continue
            if not isinstance(pending_items, list) or not pending_items:
                continue

            clean_pending: list[dict] = []
            for p in pending_items:
                item = p or {}
                code = str(item.get("booked_code") or "").strip().upper()
                parent_code = str(item.get("parent_booked_code") or "").strip().upper()
                description = str(
                    item.get("description")
                    or item.get("test_name")
                    or item.get("name")
                    or code
                ).strip()
                if not code:
                    continue
                clean_pending.append(
                    {
                        "booked_code": code,
                        "parent_booked_code": parent_code or None,
                        "description": description or code,
                    }
                )
            if not clean_pending:
                continue

            gkey = (int(booking_id), booking_patient_id, patient_id, root_booked_code)
            bucket = grouped.get(gkey)
            if not bucket:
                bucket = {
                    "booking_id": int(booking_id),
                    "booking_patient_id": booking_patient_id,
                    "patient_id": patient_id,
                    "root_booked_code": root_booked_code,
                    "root_test_name": root_test_name,
                    "items": [],
                }
                grouped[gkey] = bucket
            bucket["items"].append(
                {
                    "tube": tube_name,
                    "pending": clean_pending,
                }
            )

        for bucket in grouped.values():
            payload_json = {
                "parent": {
                    "booked_code": bucket["root_booked_code"],
                    "test_name": bucket["root_test_name"],
                },
                "items": bucket["items"],
            }
            self.db.execute(
                text(
                    """
                    INSERT INTO hcb_patient_test_pendingchildtest
                    (booking_id, booking_patient_id, patient_id, root_booked_code, root_test_name, pending_child_tests_json,
                     pending_status, source_type, appointment_id, resolved_at, resolved_by, created_at, updated_at)
                    VALUES
                    (:booking_id, :booking_patient_id, :patient_id, :root_booked_code, :root_test_name, :pending_child_tests_json,
                     0, :source_type, :appointment_id, NULL, NULL, NOW(), NOW())
                    """
                ),
                {
                    "booking_id": bucket["booking_id"],
                    "booking_patient_id": bucket["booking_patient_id"],
                    "patient_id": bucket["patient_id"],
                    "root_booked_code": bucket["root_booked_code"],
                    "root_test_name": bucket["root_test_name"],
                    "pending_child_tests_json": json.dumps(payload_json, ensure_ascii=False),
                    "source_type": src,
                    "appointment_id": appt_id,
                },
            )


    def acquire_booking_completion_lock(self, booking_id: int, wait_timeout_sec: int = 120) -> bool:
        lock_key = f"hcb_complete_{int(booking_id)}"
        try:
            row = self.db.execute(text("SELECT GET_LOCK(:k, :t) AS ok"), {"k": lock_key, "t": int(wait_timeout_sec)}).fetchone()
            ok = int(getattr(row, "ok", row[0] if row else 0) or 0)
            return ok == 1
        except Exception:
            # Fail-open only when lock primitive is unavailable.
            return False

    def release_booking_completion_lock(self, booking_id: int) -> None:
        lock_key = f"hcb_complete_{int(booking_id)}"
        try:
            self.db.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": lock_key})
        except Exception:
            return

    def acquire_global_completion_lock(self, wait_timeout_sec: int = 180) -> bool:
        lock_key = "hcb_complete_global"
        try:
            row = self.db.execute(text("SELECT GET_LOCK(:k, :t) AS ok"), {"k": lock_key, "t": int(wait_timeout_sec)}).fetchone()
            ok = int(getattr(row, "ok", row[0] if row else 0) or 0)
            return ok == 1
        except Exception:
            return False

    def release_global_completion_lock(self) -> None:
        lock_key = "hcb_complete_global"
        try:
            self.db.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": lock_key})
        except Exception:
            return

    def set_patient_payment_screenshots(self, booking_id: int, patient_id: int, rel_paths: list[str]) -> None:
        cols = self._get_table_columns("hhome_collection_booking_patient")
        if "payment_screenshot_paths" not in cols:
            return
        csv_paths = ",".join([str(x).strip() for x in (rel_paths or []) if str(x).strip()])
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_patient
                SET payment_screenshot_paths=:paths
                WHERE booking_id=:booking_id AND patient_id=:patient_id
                """
            ),
            {"paths": csv_paths or None, "booking_id": int(booking_id), "patient_id": int(patient_id)},
        )

    def apply_completion_patient_updates(
        self,
        booking_id: int,
        updates: list[dict],
        actor_user_id: int,
        include_payment_fields: bool = True,
        recompute_booking_totals: bool = True,
        include_cmplt_tube_field: bool = True,
    ) -> None:
        cols = self._get_table_columns("hhome_collection_booking_patient")
        pm_cols = self._get_table_columns("hpatient_master")
        for raw in (updates or []):
            row = raw or {}
            try:
                patient_id = int(row.get("patient_id") or 0)
            except Exception:
                patient_id = 0
            if patient_id <= 0:
                continue

            updates_sql: list[str] = []
            params: dict[str, object] = {"booking_id": int(booking_id), "patient_id": patient_id}

            if "APK_TBS" in cols and row.get("apk_tbs") is not None:
                updates_sql.append("APK_TBS=:apk_tbs")
                params["apk_tbs"] = str(row.get("apk_tbs"))
            if "report_schedule" in cols and row.get("report_schedule") is not None:
                updates_sql.append("report_schedule=:report_schedule")
                params["report_schedule"] = str(row.get("report_schedule") or "").strip() or None
            if "report_delivery" in cols:
                rv = row.get("report_delivery")
                if isinstance(rv, list):
                    rv = ",".join([str(x).strip() for x in rv if str(x).strip()])
                if rv is not None:
                    updates_sql.append("report_delivery=:report_delivery")
                    params["report_delivery"] = str(rv or "").strip() or None
            if "ref_by" in cols and row.get("referred_by") is not None:
                updates_sql.append("ref_by=:referred_by")
                params["referred_by"] = str(row.get("referred_by") or "").strip() or None
            if include_payment_fields and "payment_mode" in cols:
                pm = row.get("payment_mode")
                if isinstance(pm, list):
                    pm = ",".join([str(x).strip() for x in pm if str(x).strip()])
                if pm is not None:
                    updates_sql.append("payment_mode=:payment_mode")
                    params["payment_mode"] = str(pm or "").strip() or None
            if include_payment_fields and "payment_amount" in cols:
                pa = row.get("payment_amount")
                if isinstance(pa, list):
                    pa = ",".join([str(float(x or 0)) for x in pa])
                if pa is not None:
                    updates_sql.append("payment_amount=:payment_amount")
                    params["payment_amount"] = str(pa or "").strip() or None
            if include_payment_fields and "due_amount" in cols and row.get("due_amount") is not None:
                updates_sql.append("due_amount=:due_amount")
                params["due_amount"] = float(row.get("due_amount") or 0)
            if include_payment_fields and "extra_amount" in cols and row.get("extra_amount") is not None:
                updates_sql.append("extra_amount=:extra_amount")
                params["extra_amount"] = float(row.get("extra_amount") or 0)
            if "no_of_pricks" in cols and row.get("no_of_pricks") is not None:
                updates_sql.append("no_of_pricks=:no_of_pricks")
                params["no_of_pricks"] = str(row.get("no_of_pricks") or "").strip() or None
            if "sample_collection_is" in cols and row.get("sample_collection_is") is not None:
                updates_sql.append("sample_collection_is=:sample_collection_is")
                params["sample_collection_is"] = str(row.get("sample_collection_is") or "").strip().lower() or None
            if "additional_sample" in cols and row.get("additional_sample") is not None:
                aval = row.get("additional_sample")
                if isinstance(aval, list):
                    aval = ",".join([str(x).strip() for x in aval if str(x).strip()])
                updates_sql.append("additional_sample=:additional_sample")
                params["additional_sample"] = str(aval or "").strip() or None
            if include_cmplt_tube_field and "cmplt_tube" in cols and row.get("cmplt_tube") is not None:
                cval = row.get("cmplt_tube")
                if isinstance(cval, list):
                    cval = ",".join([str(x).strip() for x in cval if str(x).strip()])
                updates_sql.append("cmplt_tube=:cmplt_tube")
                params["cmplt_tube"] = str(cval or "").strip() or None
            if include_payment_fields and "additional_discount_amount" in cols and row.get("additional_discount_amount") is not None:
                updates_sql.append("additional_discount_amount=:additional_discount_amount")
                params["additional_discount_amount"] = float(row.get("additional_discount_amount") or 0)

            if row.get("booking_patient_status") is not None:
                updates_sql.append("booking_patient_status=:booking_patient_status")
                params["booking_patient_status"] = int(row.get("booking_patient_status") or 0)
            if "cancel_reason" in cols and row.get("cancel_reason") is not None:
                updates_sql.append("cancel_reason=:cancel_reason")
                params["cancel_reason"] = str(row.get("cancel_reason") or "").strip() or None
            if "cancel_remark" in cols and row.get("cancel_remark") is not None:
                updates_sql.append("cancel_remark=:cancel_remark")
                params["cancel_remark"] = str(row.get("cancel_remark") or "").strip() or None
            if "cancelled_by" in cols:
                # Always persist actor id for cancelled patients when caller does not pass cancelled_by.
                if int(row.get("booking_patient_status") or 0) == 4:
                    updates_sql.append("cancelled_by=:cancelled_by")
                    params["cancelled_by"] = int(row.get("cancelled_by") or actor_user_id)
                elif row.get("cancelled_by") is not None:
                    updates_sql.append("cancelled_by=:cancelled_by")
                    params["cancelled_by"] = int(row.get("cancelled_by") or actor_user_id)
            if "cancelled_at" in cols and int(row.get("booking_patient_status") or 0) == 4:
                updates_sql.append("cancelled_at=COALESCE(cancelled_at, NOW())")
            if "reschedule_requested" in cols and row.get("reschedule_requested") is not None:
                updates_sql.append("reschedule_requested=:reschedule_requested")
                params["reschedule_requested"] = 1 if bool(row.get("reschedule_requested")) else 0
            if "reschedule_date" in cols and row.get("reschedule_date") is not None:
                updates_sql.append("reschedule_date=:reschedule_date")
                params["reschedule_date"] = row.get("reschedule_date")
            if "reschedule_slot" in cols and row.get("reschedule_slot") is not None:
                updates_sql.append("reschedule_slot=:reschedule_slot")
                params["reschedule_slot"] = str(row.get("reschedule_slot") or "").strip() or None

            if updates_sql:
                self.db.execute(
                    text(
                        f"""
                        UPDATE hhome_collection_booking_patient
                        SET {', '.join(updates_sql)}
                        WHERE booking_id=:booking_id AND patient_id=:patient_id
                        """
                    ),
                    params,
                )

            if str(row.get("sample_collection_is") or "").strip().lower() == "tough":
                if "tag" in pm_cols:
                    p_row = self.db.execute(text("SELECT tag FROM hpatient_master WHERE id=:pid"), {"pid": patient_id}).fetchone()
                    existing = str(getattr(p_row, "tag", "") or "").strip()
                    tags = [x.strip() for x in existing.split(",") if x.strip()]
                    if not any(t.lower() == "tough vein" for t in tags):
                        tags.append("tough vein")
                        self.db.execute(text("UPDATE hpatient_master SET tag=:tag WHERE id=:pid"), {"tag": ",".join(tags), "pid": patient_id})

        if recompute_booking_totals:
            self._recompute_booking_amounts_from_active_tests(int(booking_id))

    def save_appointment_completed_tubes(
        self,
        *,
        booking_id: int,
        appointment_id: int,
        updates: list[dict],
    ) -> None:
        appt_cols = self._get_table_columns("hhome_collection_booking_appointment")
        if "cmplt_tube" not in appt_cols:
            return

        patients: list[dict] = []
        for raw in (updates or []):
            row = raw or {}
            if row.get("cmplt_tube") is None:
                continue
            try:
                patient_id = int(row.get("patient_id") or 0)
            except Exception:
                patient_id = 0
            if patient_id <= 0:
                continue

            try:
                booking_patient_id = int(row.get("booking_patient_id") or 0)
            except Exception:
                booking_patient_id = 0
            if booking_patient_id <= 0:
                bp_row = self.db.execute(
                    text(
                        """
                        SELECT id
                        FROM hhome_collection_booking_patient
                        WHERE booking_id=:booking_id AND patient_id=:patient_id
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"booking_id": int(booking_id), "patient_id": int(patient_id)},
                ).fetchone()
                booking_patient_id = int(getattr(bp_row, "id", 0) or 0) if bp_row else 0

            tubes = row.get("cmplt_tube")
            if isinstance(tubes, list):
                tube_list = [str(x).strip() for x in tubes if str(x).strip()]
            else:
                tube_list = [x.strip() for x in str(tubes or "").split(",") if x.strip()]
            if not tube_list:
                continue

            patients.append(
                {
                    "booking_patient_id": booking_patient_id or None,
                    "patient_id": patient_id,
                    "cmplt_tube": tube_list,
                }
            )

        if not patients:
            return

        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking_appointment
                SET cmplt_tube=:cmplt_tube,
                    updated_at=NOW()
                WHERE id=:appointment_id AND booking_id=:booking_id
                """
            ),
            {
                "cmplt_tube": json.dumps({"patients": patients}, ensure_ascii=False),
                "appointment_id": int(appointment_id),
                "booking_id": int(booking_id),
            },
        )

    def _is_manual_hcb_slip(self, value: object) -> bool:
        v = str(value or "").strip().lower()
        return v in {"manual hcb slip", "manual_hcb_slip", "manual hc slip", "manual_hc_slip", "manual_slip", "manual-slip", "hcb_slip", "hcb-slip"}

    def apply_booking_completion_patientwise(
        self,
        booking_id: int,
        updates: list[dict],
        actor_user_id: int,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> tuple[int, list[dict]]:
        rows = updates or []
        manual_patient_ids: set[int] = set()
        completed_patient_ids: set[int] = set()

        cols = self._get_table_columns("hhome_collection_booking_patient")

        for raw in rows:
            row = raw or {}
            try:
                pid = int(row.get("patient_id") or 0)
            except Exception:
                pid = 0
            if pid <= 0:
                continue

            is_manual = self._is_manual_hcb_slip(row.get("apk_tbs"))
            if is_manual:
                manual_patient_ids.add(pid)

            explicit_status = row.get("booking_patient_status")
            if explicit_status is None and is_manual:
                explicit_status = 3

            if explicit_status is None:
                continue

            try:
                bps = int(explicit_status or 0)
            except Exception:
                bps = 0

            update_bits = ["booking_patient_status=:bps"]
            params = {"bps": bps, "bid": int(booking_id), "pid": pid}

            if is_manual and "due_amount" in cols:
                update_bits.append("due_amount=0")

            self.db.execute(
                text(
                    f"""
                    UPDATE hhome_collection_booking_patient
                    SET {', '.join(update_bits)}
                    WHERE booking_id=:bid AND patient_id=:pid
                    """
                ),
                params,
            )

            if bps == 3:
                completed_patient_ids.add(pid)
            if bps == 4 and pid in completed_patient_ids:
                completed_patient_ids.remove(pid)

        # Manual-slip patients are treated as complete for pending tests and patient status.
        for pid in manual_patient_ids:
            manual_set = ["booking_patient_status=3"]
            if "due_amount" in cols:
                manual_set.append("due_amount=0")
            self.db.execute(
                text(
                    f"""
                    UPDATE hhome_collection_booking_patient
                    SET {', '.join(manual_set)}
                    WHERE booking_id=:bid AND patient_id=:pid
                    """
                ),
                {"bid": int(booking_id), "pid": pid},
            )
            completed_patient_ids.add(pid)

        if completed_patient_ids:
            self._update_pending_tests_status(
                booking_id=booking_id,
                to_status=1,
                patient_ids=sorted(completed_patient_ids),
            )

        counts = self.get_booking_patient_status_counts(booking_id)
        total = sum(counts.values())
        all_completed = total > 0 and counts.get(3, 0) == total
        has_completed = counts.get(3, 0) > 0
        has_non_completed = total > counts.get(3, 0)
        all_cancelled = total > 0 and counts.get(4, 0) == total

        if all_completed:
            final_status = 3
        elif all_cancelled:
            final_status = 4
        elif has_completed and has_non_completed:
            final_status = 5
        else:
            # Keep booking pending/assigned/start as-is when no patient completed yet.
            cur = self.db.execute(text("SELECT booking_status FROM hhome_collection_booking WHERE id=:bid"), {"bid": int(booking_id)}).fetchone()
            final_status = int((cur.booking_status if cur else 0) or 0)

        self.db.execute(
            text("UPDATE hhome_collection_booking SET booking_status=:st WHERE id=:bid"),
            {"st": int(final_status), "bid": int(booking_id)},
        )
        if final_status == 3:
            self._apply_booking_lifecycle_fields(
                booking_id=int(booking_id),
                action="complete",
                complete_time=complete_time,
                complete_location=complete_location,
            )
        self.db.commit()

        patient_rows = self.get_booking_patient_status_rows(booking_id)
        return final_status, patient_rows

    def handle_cancelled_patient_reschedule(self, booking_id: int, updates: list[dict], actor_user_id: int) -> None:
        booking_cols = self._get_table_columns("hhome_collection_booking")
        patient_cols = self._get_table_columns("hhome_collection_booking_patient")
        has_ref = "bkg_ref_flag" in booking_cols
        src = self.db.execute(
            text("SELECT id, caller_id, selected_address_id, assigned_phlebotomist_id, address_snapshot_json FROM hhome_collection_booking WHERE id=:bid LIMIT 1"),
            {"bid": int(booking_id)},
        ).fetchone()
        if not src:
            return

        any_cancel_processed = False

        for raw in (updates or []):
            row = raw or {}
            try:
                pid = int(row.get("patient_id") or 0)
            except Exception:
                pid = 0
            if pid <= 0:
                continue
            is_cancelled = int(row.get("booking_patient_status") or 0) == 4
            wants_reschedule = bool(row.get("reschedule_requested"))
            if not is_cancelled:
                continue

            # Drop active tests in original booking for cancelled patient.
            any_cancel_processed = True
            self.db.execute(
                text(
                    """
                    UPDATE hhome_collection_booking_patient_test
                    SET test_status='2', dropped_at=COALESCE(dropped_at, NOW()), dropped_by=COALESCE(dropped_by, :uid)
                    WHERE booking_id=:bid AND patient_id=:pid AND (test_status IS NULL OR TRIM(test_status)='' OR test_status='0' OR UPPER(TRIM(test_status))='PENDING')
                    """
                ),
                {"uid": int(actor_user_id), "bid": int(booking_id), "pid": int(pid)},
            )

            if not wants_reschedule:
                continue

            res_date = row.get("reschedule_date")
            res_slot = str(row.get("reschedule_slot") or "").strip() or None
            if not res_date or not res_slot:
                continue

            params = {
                "caller_id": int(src.caller_id or 0) or None,
                "selected_address_id": int(src.selected_address_id or 0) or None,
                "preferred_visit_date": res_date,
                "preferred_time_slot": res_slot,
                "booking_status": 0,
                "assigned_phlebotomist_id": None,
                "created_by": int(actor_user_id),
                "address_snapshot_json": (src.address_snapshot_json if getattr(src, "address_snapshot_json", None) else "{}"),
            }
            cols = ["caller_id", "selected_address_id", "address_snapshot_json", "preferred_visit_date", "preferred_time_slot", "booking_status", "assigned_phlebotomist_id", "created_by"]
            vals = [":caller_id", ":selected_address_id", ":address_snapshot_json", ":preferred_visit_date", ":preferred_time_slot", ":booking_status", ":assigned_phlebotomist_id", ":created_by"]
            if "booking_code" in booking_cols:
                cols.append("booking_code")
                vals.append(":booking_code")
                params["booking_code"] = f"HC26-PENDING-{int(booking_id)}-{int(pid)}"
            if has_ref:
                cols.append("bkg_ref_flag")
                vals.append(":bkg_ref_flag")
                params["bkg_ref_flag"] = int(booking_id)
            ins = self.db.execute(text(f"INSERT INTO hhome_collection_booking ({', '.join(cols)}) VALUES ({', '.join(vals)})"), params)
            new_booking_id = int(ins.lastrowid)
            new_code = f"HC26-{new_booking_id}"
            self.db.execute(text("UPDATE hhome_collection_booking SET booking_code=:code WHERE id=:bid"), {"code": new_code, "bid": new_booking_id})

            bp_status = 0
            src_bp = self.db.execute(
                text(
                    """
                    SELECT cce_level_TBS, selected_comp_cat_ids, selected_charge_modes, selected_panel_companies, additional_discount_amount
                    FROM hhome_collection_booking_patient
                    WHERE booking_id=:bid AND patient_id=:pid
                    LIMIT 1
                    """
                ),
                {"bid": int(booking_id), "pid": int(pid)},
            ).mappings().first() or {}
            bp_cols = ["booking_id", "patient_id", "booking_patient_status", "created_by"]
            bp_vals = [":bid", ":pid", ":st", ":uid"]
            bp_params = {"bid": new_booking_id, "pid": int(pid), "st": bp_status, "uid": int(actor_user_id)}
            patient_table_cols = self._get_table_columns("hhome_collection_booking_patient")
            if "cce_level_TBS" in patient_table_cols:
                bp_cols.append("cce_level_TBS")
                bp_vals.append(":cce_level_tbs")
                bp_params["cce_level_tbs"] = src_bp.get("cce_level_TBS")
            if "selected_comp_cat_ids" in patient_table_cols:
                bp_cols.append("selected_comp_cat_ids")
                bp_vals.append(":selected_comp_cat_ids")
                bp_params["selected_comp_cat_ids"] = src_bp.get("selected_comp_cat_ids")
            if "selected_charge_modes" in patient_table_cols:
                bp_cols.append("selected_charge_modes")
                bp_vals.append(":selected_charge_modes")
                bp_params["selected_charge_modes"] = src_bp.get("selected_charge_modes")
            if "selected_panel_companies" in patient_table_cols:
                bp_cols.append("selected_panel_companies")
                bp_vals.append(":selected_panel_companies")
                bp_params["selected_panel_companies"] = src_bp.get("selected_panel_companies")
            if "additional_discount_amount" in patient_table_cols:
                bp_cols.append("additional_discount_amount")
                bp_vals.append(":additional_discount_amount")
                bp_params["additional_discount_amount"] = float(src_bp.get("additional_discount_amount") or 0)

            new_bp = self.db.execute(
                text(
                    f"""
                    INSERT INTO hhome_collection_booking_patient ({', '.join(bp_cols)})
                    VALUES ({', '.join(bp_vals)})
                    """
                ),
                bp_params,
            )
            new_bp_id = int(new_bp.lastrowid)

            # Copy dropped tests as active tests into new rescheduled booking.
            tests = self.db.execute(
                text(
                    """
                    SELECT comp_cat_id, booked_code, test_name, charge, mrp, max_discount
                    FROM hhome_collection_booking_patient_test
                    WHERE booking_id=:bid AND patient_id=:pid
                    """
                ),
                {"bid": int(booking_id), "pid": int(pid)},
            ).mappings().all()
            for t in tests:
                self.db.execute(
                    text(
                        """
                        INSERT INTO hhome_collection_booking_patient_test
                        (booking_id, booking_patient_id, patient_id, comp_cat_id, booked_code, test_name, charge, mrp, max_discount, test_status, created_by)
                        VALUES (:bid, :bpid, :pid, :cc, :bc, :tn, :ch, :mrp, :md, '0', :uid)
                        """
                    ),
                    {
                        "bid": new_booking_id,
                        "bpid": new_bp_id,
                        "pid": int(pid),
                        "cc": t.get("comp_cat_id"),
                        "bc": t.get("booked_code"),
                        "tn": t.get("test_name"),
                        "ch": float(t.get("charge") or 0),
                        "mrp": float(t.get("mrp") or 0),
                        "md": float(t.get("max_discount") or 0),
                        "uid": int(actor_user_id),
                    },
                )

            # Recompute new rescheduled booking amount summary from its active tests.
            self._recompute_booking_amounts_from_active_tests(new_booking_id)

        # Recompute original booking only when this handler actually processed cancelled patient changes.
        if any_cancel_processed:
            self._recompute_booking_amounts_from_active_tests(int(booking_id))

    def create_auto_followup_appointment(
        self,
        booking_id: int,
        actor_user_id: int,
        preferred_date,
        preferred_slot,
        selected_patient_ids: list[int] | None = None,
        appointment_tests_snapshot: dict | None = None,
    ) -> None:
        cols = self._get_appointment_columns()
        if not cols:
            return

        src = self.db.execute(
            text("SELECT preferred_visit_date, assigned_phlebotomist_id, selected_address_id, address_snapshot_json FROM hhome_collection_booking WHERE id=:bid LIMIT 1"),
            {"bid": int(booking_id)},
        ).fetchone()

        src_date = str(getattr(src, "preferred_visit_date", "") or "").strip()
        followup_date = str(preferred_date or "").strip()
        src_assigned = int(getattr(src, "assigned_phlebotomist_id", 0) or 0)
        should_assign_same_date = bool(src_assigned > 0 and src_date and followup_date and src_date == followup_date)

        # Idempotency guard: avoid duplicate follow-up appointment on quick retry/double-submit.
        if "preferred_visit_date" in cols and "preferred_time_slot" in cols:
            dup = self.db.execute(
                text(
                    """
                    SELECT id
                    FROM hhome_collection_booking_appointment
                    WHERE booking_id = :booking_id
                      AND preferred_visit_date = :preferred_visit_date
                      AND preferred_time_slot = :preferred_time_slot
                      AND appointment_status IN (0, 1, 2)
                      AND created_at IS NOT NULL
                      AND TIMESTAMPDIFF(SECOND, created_at, NOW()) BETWEEN 0 AND 180
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {
                    "booking_id": int(booking_id),
                    "preferred_visit_date": preferred_date,
                    "preferred_time_slot": str(preferred_slot),
                },
            ).fetchone()
            if dup:
                return

        insert_cols = []
        values = []
        params = {}

        if "booking_id" in cols:
            insert_cols.append("booking_id")
            values.append(":booking_id")
            params["booking_id"] = int(booking_id)
        if "appointment_no" in cols:
            next_no_row = self.db.execute(
                text("SELECT COALESCE(MAX(appointment_no), 0) + 1 AS n FROM hhome_collection_booking_appointment WHERE booking_id=:bid"),
                {"bid": int(booking_id)},
            ).fetchone()
            next_no = int(getattr(next_no_row, "n", next_no_row[0] if next_no_row else 1) or 1)
            insert_cols.append("appointment_no")
            values.append(":appointment_no")
            params["appointment_no"] = next_no
        if "selected_address_id" in cols:
            insert_cols.append("selected_address_id")
            values.append(":selected_address_id")
            params["selected_address_id"] = int(getattr(src, "selected_address_id", 0) or 0) or None
        if "address_snapshot_json" in cols:
            insert_cols.append("address_snapshot_json")
            values.append(":address_snapshot_json")
            params["address_snapshot_json"] = str(getattr(src, "address_snapshot_json", None) or "{}")
        if "selected_patient_ids_json" in cols:
            insert_cols.append("selected_patient_ids_json")
            values.append(":selected_patient_ids_json")
            clean_ids = sorted({int(x) for x in (selected_patient_ids or []) if int(x or 0) > 0})
            params["selected_patient_ids_json"] = json.dumps(clean_ids, ensure_ascii=True) if clean_ids else None
        if "appointment_tests_snapshot_json" in cols:
            insert_cols.append("appointment_tests_snapshot_json")
            values.append(":appointment_tests_snapshot_json")
            params["appointment_tests_snapshot_json"] = json.dumps((appointment_tests_snapshot or {}), ensure_ascii=False)
        if "payment_snapshot_json" in cols:
            insert_cols.append("payment_snapshot_json")
            values.append(":payment_snapshot_json")
            clean_ids = sorted({int(x) for x in (selected_patient_ids or []) if int(x or 0) > 0})
            params["payment_snapshot_json"] = json.dumps(
                {
                    "payments": [],
                    "payment_screenshots": {},
                    "summary": {
                        "sub_total": 0.0,
                        "credit_amount": 0.0,
                        "paying_amount": 0.0,
                        "base_discount": 0.0,
                        "additional_discount": 0.0,
                        "final_discount": 0.0,
                        "total_amount": 0.0,
                    },
                    "patient_context": self._build_appointment_patient_context(
                        booking_id=int(booking_id),
                        patient_ids=clean_ids,
                        default_status=0,
                        existing_context={},
                    ),
                },
                ensure_ascii=False,
            )
        if "preferred_visit_date" in cols and preferred_date is not None:
            insert_cols.append("preferred_visit_date")
            values.append(":preferred_visit_date")
            params["preferred_visit_date"] = preferred_date
        if "preferred_time_slot" in cols and preferred_slot is not None:
            insert_cols.append("preferred_time_slot")
            values.append(":preferred_time_slot")
            params["preferred_time_slot"] = str(preferred_slot)
        if "assigned_phlebotomist_id" in cols:
            insert_cols.append("assigned_phlebotomist_id")
            values.append(":assigned_phlebotomist_id")
            params["assigned_phlebotomist_id"] = (src_assigned if should_assign_same_date else None)
        if "appointment_status" in cols:
            insert_cols.append("appointment_status")
            values.append(":appointment_status")
            params["appointment_status"] = 0
        if "created_by" in cols:
            insert_cols.append("created_by")
            values.append(":created_by")
            params["created_by"] = int(actor_user_id)

        if not insert_cols:
            return
        self.db.execute(
            text(f"INSERT INTO hhome_collection_booking_appointment ({', '.join(insert_cols)}) VALUES ({', '.join(values)})"),
            params,
        )

    def _recompute_booking_amounts_from_active_tests(self, booking_id: int) -> None:
        active_expr = "CASE WHEN test_status IS NULL OR TRIM(test_status)='' THEN 0 WHEN UPPER(TRIM(test_status)) IN ('PENDING','0') THEN 0 WHEN UPPER(TRIM(test_status)) IN ('COMPLETED','1') THEN 1 WHEN UPPER(TRIM(test_status)) IN ('DROPPED','CANCELLED','2') THEN 2 ELSE 0 END"
        rows = self.db.execute(
            text(
                f"""
                SELECT t.patient_id, COALESCE(t.mrp,0) AS mrp, COALESCE(t.max_discount,0) AS max_discount
                FROM hhome_collection_booking_patient_test t
                WHERE t.booking_id=:booking_id AND {active_expr}=0
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().all()
        patient_amount_rows = self.db.execute(
            text(
                f"""
                SELECT t.patient_id, COALESCE(t.charge,0) AS charge
                FROM hhome_collection_booking_patient_test t
                WHERE t.booking_id=:booking_id AND {active_expr} IN (0,1)
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().all()

        patient_modes = self.db.execute(
            text(
                """
                SELECT patient_id, selected_charge_modes, additional_discount_amount, booking_patient_status
                FROM hhome_collection_booking_patient
                WHERE booking_id=:booking_id
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().all()
        mode_map = {int(r.get("patient_id") or 0): str(r.get("selected_charge_modes") or "").upper() for r in patient_modes}
        patient_addl_map = {int(r.get("patient_id") or 0): float(r.get("additional_discount_amount") or 0) for r in patient_modes}
        addl_total = sum(
            float(r.get("additional_discount_amount") or 0)
            for r in patient_modes
            if int(r.get("booking_patient_status") or 0) != 4
        )

        patient_charge_totals: dict[int, float] = {}
        for r in patient_amount_rows:
            pid = int(r.get("patient_id") or 0)
            if pid <= 0:
                continue
            patient_charge_totals[pid] = round(float(patient_charge_totals.get(pid) or 0) + float(r.get("charge") or 0), 2)

        subtotal = 0.0
        base_discount = 0.0
        credit_amount = 0.0
        paying_amount = 0.0
        for r in rows:
            pid = int(r.get("patient_id") or 0)
            mrp = float(r.get("mrp") or 0)
            md = float(r.get("max_discount") or 0)
            subtotal += mrp
            base_discount += md
            mode = mode_map.get(pid, "")
            if ("C" in mode) and ("P" not in mode):
                credit_amount += mrp
            else:
                paying_amount += max(0.0, mrp - md)

        final_discount = base_discount + float(addl_total or 0)
        total_amount = max(0.0, subtotal - final_discount)

        patient_cols = self._get_table_columns("hhome_collection_booking_patient")
        if "patient_final_amount" in patient_cols:
            for pid, charge_total in patient_charge_totals.items():
                addl = float(patient_addl_map.get(pid) or 0)
                patient_final_amount = round(max(0.0, charge_total - addl), 2)
                self.db.execute(
                    text(
                        """
                        UPDATE hhome_collection_booking_patient
                        SET patient_final_amount=:patient_final_amount
                        WHERE booking_id=:booking_id AND patient_id=:patient_id
                        """
                    ),
                    {
                        "patient_final_amount": patient_final_amount,
                        "booking_id": int(booking_id),
                        "patient_id": int(pid),
                    },
                )

        booking_cols = self._get_table_columns("hhome_collection_booking")
        if {"credit_amount", "paying_amount"}.issubset(booking_cols):
            self.db.execute(
                text(
                    """
                    UPDATE hhome_collection_booking
                    SET F_Apt_Am=:sub_total,
                        credit_amount=:credit_amount,
                        paying_amount=:paying_amount,
                        F_dis=:f_dis,
                        Ad_Dis=:ad_dis,
                        total_amount=:total_amount
                    WHERE id=:booking_id
                    """
                ),
                {
                    "sub_total": round(float(subtotal or 0), 2),
                    "credit_amount": round(float(credit_amount or 0), 2),
                    "paying_amount": round(float(paying_amount or 0), 2),
                    "f_dis": round(float(final_discount or 0), 2),
                    "ad_dis": round(float(addl_total or 0), 2),
                    "total_amount": round(float(total_amount or 0), 2),
                    "booking_id": int(booking_id),
                },
            )
        else:
            self.db.execute(
                text(
                    """
                    UPDATE hhome_collection_booking
                    SET F_Apt_Am=:sub_total,
                        F_dis=:f_dis,
                        Ad_Dis=:ad_dis,
                        total_amount=:total_amount
                    WHERE id=:booking_id
                    """
                ),
                {
                    "sub_total": round(float(subtotal or 0), 2),
                    "f_dis": round(float(final_discount or 0), 2),
                    "ad_dis": round(float(addl_total or 0), 2),
                    "total_amount": round(float(total_amount or 0), 2),
                    "booking_id": int(booking_id),
                },
            )




    def insert_hhome_collection_batch(
        self,
        *,
        batch_json: dict,
        booking_ids: list[int],
        patients_json: list[dict],
        tubes_json: list[dict],
        created_by: int,
    ) -> int:
        res = self.db.execute(
            text(
                "INSERT INTO hhome_collection_batch (batch_json, booking_ids, patients_json, tubes_json, created_by) VALUES (:batch_json, :booking_ids, :patients_json, :tubes_json, :created_by)"
            ),
            {
                "batch_json": json.dumps(batch_json or {}, ensure_ascii=False),
                "booking_ids": json.dumps([int(x) for x in (booking_ids or [])], ensure_ascii=False),
                "patients_json": json.dumps(patients_json or [], ensure_ascii=False),
                "tubes_json": json.dumps(tubes_json or [], ensure_ascii=False),
                "created_by": int(created_by),
            },
        )
        batch_id = int(getattr(res, "lastrowid", 0) or 0)
        if batch_id > 0 and "batch_code" in self._get_table_columns("hhome_collection_batch"):
            batch_code = f"BATCH{datetime.now():%y}-{batch_id}"
            self.db.execute(
                text("UPDATE hhome_collection_batch SET batch_code=:batch_code WHERE id=:batch_id"),
                {"batch_code": batch_code, "batch_id": batch_id},
            )
        self.db.commit()
        return batch_id

    def list_hhome_collection_batch_for_user(
        self,
        *,
        created_by: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        rows = self.db.execute(
            text(
                """
                SELECT id, batch_code, batch_json, booking_ids, patients_json, tubes_json, created_at
                FROM hhome_collection_batch
                WHERE created_by = :created_by
                  AND created_at >= TIMESTAMP(DATE(DATE_SUB(NOW(), INTERVAL 4 HOUR)), '04:00:00')
                  AND created_at < DATE_ADD(TIMESTAMP(DATE(DATE_SUB(NOW(), INTERVAL 4 HOUR)), '04:00:00'), INTERVAL 1 DAY)
                ORDER BY id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "created_by": int(created_by),
                "limit": int(max(1, min(limit, 200))),
                "offset": int(max(0, offset)),
            },
        ).mappings().all()
        return [dict(x) for x in rows]

    def list_hhome_collection_batch_payloads_for_user(self, *, created_by: int) -> list[dict]:
        rows = self.db.execute(
            text(
                """
                SELECT patients_json, tubes_json
                FROM hhome_collection_batch
                WHERE created_by = :created_by
                ORDER BY id DESC
                LIMIT 1000
                """
            ),
            {"created_by": int(created_by)},
        ).mappings().all()
        return [dict(x) for x in rows]

    def list_completed_booking_patients_for_batch(self, *, user_id: int) -> list[dict]:
        rows = self.db.execute(
            text(
                """
                SELECT
                    b.id AS booking_id,
                    NULL AS appointment_id,
                    'BOOKING' AS source_type,
                    bp.id AS booking_patient_id,
                    bp.patient_id,
                    NULLIF(TRIM(pm.full_name), '') AS patient_name,
                    bp.cmplt_tube,
                    bp.additional_sample
                FROM hhome_collection_booking b
                JOIN hhome_collection_booking_patient bp ON bp.booking_id = b.id
                LEFT JOIN hpatient_master pm ON pm.id = bp.patient_id
                WHERE b.assigned_phlebotomist_id = :user_id
                  AND b.booking_status = 3
                  AND COALESCE(bp.booking_patient_status, 0) = 3
                  AND (
                    NULLIF(TRIM(COALESCE(bp.cmplt_tube, '')), '') IS NOT NULL
                    OR NULLIF(TRIM(COALESCE(bp.additional_sample, '')), '') IS NOT NULL
                  )
                ORDER BY b.id DESC, bp.id
                LIMIT 500
                """
            ),
            {"user_id": int(user_id)},
        ).mappings().all()
        return [dict(x) for x in rows]

    def list_completed_appointments_for_batch(self, *, user_id: int) -> list[dict]:
        rows = self.db.execute(
            text(
                """
                SELECT
                    b.id AS booking_id,
                    ap.id AS appointment_id,
                    'APPOINTMENT' AS source_type,
                    ap.cmplt_tube
                FROM hhome_collection_booking_appointment ap
                JOIN hhome_collection_booking b ON b.id = ap.booking_id
                WHERE ap.assigned_phlebotomist_id = :user_id
                  AND COALESCE(ap.appointment_status, 0) = 3
                  AND NULLIF(TRIM(COALESCE(ap.cmplt_tube, '')), '') IS NOT NULL
                ORDER BY ap.id DESC
                LIMIT 500
                """
            ),
            {"user_id": int(user_id)},
        ).mappings().all()
        return [dict(x) for x in rows]

    def list_booking_patients_for_bookings(self, *, booking_ids: list[int]) -> list[dict]:
        ids = sorted({int(x) for x in (booking_ids or []) if int(x) > 0})
        if not ids:
            return []
        rows = self.db.execute(
            text(
                """
                SELECT
                    bp.booking_id,
                    bp.id AS booking_patient_id,
                    bp.patient_id,
                    NULLIF(TRIM(pm.full_name), '') AS patient_name
                FROM hhome_collection_booking_patient bp
                LEFT JOIN hpatient_master pm ON pm.id = bp.patient_id
                WHERE bp.booking_id IN :booking_ids
                """
            ).bindparams(bindparam("booking_ids", expanding=True)),
            {"booking_ids": ids},
        ).mappings().all()
        return [dict(x) for x in rows]

    def get_booking_address_context(self, booking_id: int) -> dict | None:
        row = self.db.execute(
            text(
                """
                SELECT id, selected_address_id, address_snapshot_json
                FROM hhome_collection_booking
                WHERE id=:booking_id
                LIMIT 1
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings().first()
        return dict(row) if row else None

    def update_booking_address(
        self,
        booking_id: int,
        address_id: int,
        fields: dict,
    ) -> dict | None:
        cols = self._get_table_columns("haddress_master")
        set_parts: list[str] = []
        params: dict[str, object] = {"address_id": int(address_id)}

        mapping = {
            "address_type": "address_type",
            "house_flat_no": "house_flat_no",
            "floor": "floor",
            "block_tower_no": "block_tower_no",
            "street_line": "street_line",
            "landmark": "landmark",
            "colony_name": "colony_name",
            "pincode": "pincode",
            "route_no": "route_no",
            "city": "city",
            "google_location": "google_location",
            "access_notes": "access_notes",
        }

        for key, col in mapping.items():
            if col in cols and key in fields:
                set_parts.append(f"{col}=:{key}")
                val = fields.get(key)
                if isinstance(val, str):
                    val = val.strip() or None
                params[key] = val

        if set_parts:
            self.db.execute(
                text(
                    f"""
                    UPDATE haddress_master
                    SET {', '.join(set_parts)}
                    WHERE id=:address_id
                    """
                ),
                params,
            )

        updated = self.get_address(address_id)
        if not updated:
            return None
        self.db.execute(
            text(
                """
                UPDATE hhome_collection_booking
                SET address_snapshot_json=:snapshot
                WHERE id=:booking_id
                """
            ),
            {"snapshot": json.dumps(updated, ensure_ascii=False), "booking_id": int(booking_id)},
        )
        self.db.commit()
        return updated
