from pathlib import Path
from datetime import date, datetime
import json
import logging
from time import perf_counter
from collections import defaultdict

from fastapi import HTTPException, status
from sqlalchemy import bindparam, text
import re
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.booking_repository import BookingRepository
from app.services.service_note_pdf_generator import submit_pdf_generation
from app.schemas.booking import (
    AddPatientToBookingRequest,
    AddPatientToBookingResponse,
    AddressDetails,
    BookingAmounts,
    BookingDetailsResponse,
    BookingStatusUpdateResponse,
    BookingSummary,
    EditPatientInBookingRequest,
    EditPatientInBookingResponse,
    EditBookingAddressRequest,
    EditBookingAddressResponse,
    LinkedPatientDetails,
    MobileBookingTestsSaveRequest,
    MobileBookingTestsSaveResponse,
    PatientDetails,
    BatchBookingItem,
    BatchPatientItem,
    BatchReadyResponse,
    BatchSaveRequest,
    BatchSaveResponse,
    BatchListItem,
    BatchListResponse,
    BatchTubeItem,
)


class BookingService:
    @staticmethod
    def _split_csv_values(raw: object) -> list[str]:
        if raw is None:
            return []
        src = str(raw).strip()
        if not src:
            return []
        return [x.strip() for x in src.split(',') if x and x.strip()]

    @staticmethod
    def _merge_csv_values(*values: object) -> str | None:
        merged: list[str] = []
        seen: set[str] = set()
        for value in values:
            for item in BookingService._split_csv_values(value):
                key = item.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item.strip())
        return ", ".join(merged) if merged else None

    _allowed_document_ext = {".pdf", ".jpg", ".jpeg", ".png"}
    _max_documents_per_patient = 5

    def __init__(self, repository: BookingRepository) -> None:
        self.repository = repository
        self._settings = get_settings()
        self._logger = logging.getLogger(__name__)

    @staticmethod
    def _normalize_mobile(value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if len(digits) != 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid mobile number format",
            )
        return digits

    @staticmethod
    def _as_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _to_float(value: object) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_date(value: object):
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        text = str(value).strip()
        if not text or text in {"0000-00-00", "0000-00-00 00:00:00"}:
            return None
        try:
            return date.fromisoformat(text[:10])
        except (TypeError, ValueError):
            return None

    def _public_upload_url(self, relative_path: str) -> str:
        rel = "/" + str(relative_path or "").strip().lstrip("/")
        base = str(self._settings.main_web_domain or "").strip().rstrip("/")
        if not base:
            return rel
        return f"{base}{rel}"

    @staticmethod
    def _safe_json_dict(raw_value: object) -> dict:
        if isinstance(raw_value, dict):
            return raw_value
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else {}
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _merge_address_snapshot(self, base_address: dict | None, snapshot_raw: object) -> dict:
        address = dict(base_address or {})
        snapshot = self._safe_json_dict(snapshot_raw)
        if not snapshot:
            return address
        for key in (
            "id",
            "address_type",
            "house_flat_no",
            "floor",
            "block_tower_no",
            "street_line",
            "landmark",
            "colony_name",
            "colony_name_snapshot",
            "city",
            "pincode",
            "pincode_snapshot",
            "route_no",
            "route_no_snapshot",
            "google_location",
            "location_url",
            "access_notes",
        ):
            value = snapshot.get(key)
            if self._as_str(value) is not None:
                address[key] = value
        if self._as_str(address.get("colony_name_snapshot")) is None:
            address["colony_name_snapshot"] = address.get("colony_name")
        if self._as_str(address.get("pincode_snapshot")) is None:
            address["pincode_snapshot"] = address.get("pincode")
        if self._as_str(address.get("route_no_snapshot")) is None:
            address["route_no_snapshot"] = address.get("route_no")
        if self._as_str(address.get("location_url")) is None:
            address["location_url"] = address.get("google_location")
        return address

    def _panel_meta_for_patient_row(self, row: dict) -> dict[str, dict[str, str | None]]:
        comp_ids = [self._as_str(x) for x in self._split_csv_values(row.get("selected_comp_cat_ids"))]
        charge_modes = [self._as_str(x) for x in self._split_csv_values(row.get("selected_charge_modes"))]
        panel_names = [self._as_str(x) for x in self._split_csv_values(row.get("selected_panel_companies"))]
        meta: dict[str, dict[str, str | None]] = {}
        for idx, comp_cat_id in enumerate(comp_ids):
            if not comp_cat_id:
                continue
            meta[comp_cat_id] = {
                "panel_company": panel_names[idx] if idx < len(panel_names) else self._as_str(row.get("panel_company")),
                "selected_charge_mode": charge_modes[idx] if idx < len(charge_modes) else None,
            }
        return meta

    def _group_tests_into_panels(self, tests: list[dict], patient_row: dict) -> list[dict]:
        panel_meta = self._panel_meta_for_patient_row(patient_row)
        grouped: dict[tuple[str | None, str | None, str | None], list[dict]] = defaultdict(list)
        for test in tests or []:
            comp_cat_id = self._as_str(test.get("comp_cat_id"))
            meta = panel_meta.get(comp_cat_id or "") if comp_cat_id else None
            panel_company = self._as_str(test.get("panel_company")) or (meta or {}).get("panel_company") or self._as_str(patient_row.get("panel_company"))
            selected_charge_mode = (meta or {}).get("selected_charge_mode")
            key = (panel_company, comp_cat_id, selected_charge_mode)
            grouped[key].append(
                {
                    "booked_code": self._as_str(test.get("booked_code")),
                    "description": self._as_str(test.get("test_name") or test.get("description")) or self._as_str(test.get("booked_code")) or "-",
                    "charge": self._to_float(test.get("charge")),
                    "mrp": self._to_float(test.get("mrp")),
                    "max_discount": self._to_float(test.get("max_discount")),
                    "max_allowed_discount": self._to_float(test.get("max_allowed_discount")),
                    "test_status": test.get("test_status"),
                }
            )
        out: list[dict] = []
        for (panel_company, comp_cat_id, selected_charge_mode), selected_tests in grouped.items():
            out.append(
                {
                    "panel_company": panel_company,
                    "comp_cat_id": comp_cat_id,
                    "selected_charge_mode": selected_charge_mode,
                    "selected_tests": selected_tests,
                }
            )
        return out

    def _build_tests_from_appointment_snapshot(
        self,
        snapshot_raw: str | None,
        patient_ids: list[int] | None = None,
    ) -> dict[int, list[dict]]:
        if not snapshot_raw:
            return {}
        try:
            payload = json.loads(snapshot_raw)
        except Exception:
            return {}
        tests_map = payload.get("tests_billing_map") or {}
        pending_map = payload.get("pending_tests_map") or {}
        parent_context_map = payload.get("parent_context_map") or {}
        allowed = {int(x) for x in (patient_ids or []) if str(x).isdigit()} if patient_ids else None
        result: dict[int, list[dict]] = {}

        def _iter_sections(node: dict) -> list[tuple[dict, dict, list[dict]]]:
            if not isinstance(node, dict):
                return []
            panels = node.get("panels") or []
            sections: list[tuple[dict, dict, list[dict]]] = []
            if isinstance(panels, list) and panels:
                for sec in panels:
                    if not isinstance(sec, dict):
                        continue
                    sections.append((
                        sec.get("panel") or {},
                        sec.get("billing") or {},
                        list(sec.get("selected_tests") or []),
                    ))
            else:
                sections.append((
                    node.get("panel") or {},
                    node.get("billing") or {},
                    list(node.get("selected_tests") or []),
                ))
            return sections

        all_keys = set(tests_map.keys()) | set(pending_map.keys()) | set(parent_context_map.keys())
        for pid_key in all_keys:
            try:
                pid = int(pid_key)
            except Exception:
                continue
            if allowed is not None and pid not in allowed:
                continue

            tests_out: list[dict] = []
            seen_codes: set[str] = set()

            # Appointment response rule: show pending child tests first.
            pending_node = pending_map.get(pid_key) or pending_map.get(str(pid_key)) or {}
            has_pending_rows = False
            for panel_meta, billing_meta, selected_rows in _iter_sections(pending_node):
                panel_company = self._as_str(panel_meta.get("pname"))
                comp_cat_id = self._as_str(billing_meta.get("comp_cat_id"))
                for item in selected_rows:
                    code = self._as_str(item.get("booked_code"))
                    if not code:
                        continue
                    has_pending_rows = True
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    tests_out.append({
                        "booked_code": code,
                        "comp_cat_id": comp_cat_id,
                        "panel_company": panel_company,
                        "test_name": self._as_str(item.get("description")) or self._as_str(item.get("test_name")) or code,
                        "test_status": 0,
                        "mrp": self._to_float(item.get("mrp")),
                        "charge": self._to_float(item.get("charge")),
                        "max_discount": self._to_float(item.get("max_discount")),
                    })

            if has_pending_rows:
                result[pid] = tests_out
                continue

            # Fallback: selected/parent tests from tests_billing_map.
            tests_node = tests_map.get(pid_key) or tests_map.get(str(pid_key)) or {}
            for panel_meta, billing_meta, selected_rows in _iter_sections(tests_node):
                panel_company = self._as_str(panel_meta.get("pname"))
                comp_cat_id = self._as_str(billing_meta.get("comp_cat_id"))
                for item in selected_rows:
                    code = self._as_str(item.get("booked_code"))
                    if not code or code in seen_codes:
                        continue
                    seen_codes.add(code)
                    tests_out.append({
                        "booked_code": code,
                        "comp_cat_id": comp_cat_id,
                        "panel_company": panel_company,
                        "test_name": self._as_str(item.get("description")) or self._as_str(item.get("test_name")) or code,
                        "test_status": 0,
                        "mrp": self._to_float(item.get("mrp")),
                        "charge": self._to_float(item.get("charge")),
                        "max_discount": self._to_float(item.get("max_discount")),
                    })

            result[pid] = tests_out

        return result

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

    def _build_appointment_payment_snapshot(
        self,
        appointment_snapshot_raw: str | None,
        patient_updates: list[dict] | None,
        payment_screenshot_paths_by_patient: dict[int, list[str]] | None = None,
        patient_ids: list[int] | None = None,
        existing_payment_snapshot_raw: str | None = None,
    ) -> dict:
        tests_by_patient = self._build_tests_from_appointment_snapshot(
            appointment_snapshot_raw,
            patient_ids=patient_ids,
        )
        update_map: dict[int, dict] = {}
        for row in (patient_updates or []):
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("patient_id") or 0)
            except Exception:
                pid = 0
            if pid > 0:
                update_map[pid] = row

        payment_rows: list[dict] = []
        payment_screenshots = {}
        sub_total = 0.0
        base_discount = 0.0
        charge_total = 0.0
        additional_discount = 0.0

        for pid, tests in (tests_by_patient or {}).items():
            patient_sub_total = 0.0
            patient_base_discount = 0.0
            patient_charge_total = 0.0
            for test in (tests or []):
                patient_sub_total += self._to_float(test.get("mrp"))
                patient_base_discount += self._to_float(test.get("max_discount"))
                patient_charge_total += self._to_float(test.get("charge"))
            row = update_map.get(int(pid)) or {}
            _pmode = row.get("payment_mode")
            if isinstance(_pmode, list):
                _pmode = _pmode[0] if _pmode else None
            _pamt = row.get("payment_amount")
            if isinstance(_pamt, list):
                _pamt = _pamt[0] if _pamt else 0
            patient_additional = self._to_float(row.get("additional_discount_amount"))
            patient_total = max(0.0, patient_charge_total - patient_additional)
            sub_total += patient_sub_total
            base_discount += patient_base_discount
            charge_total += patient_charge_total
            additional_discount += patient_additional
            screenshots = [str(x).strip() for x in (payment_screenshot_paths_by_patient or {}).get(int(pid), []) if str(x).strip()]
            if screenshots:
                payment_screenshots[str(int(pid))] = screenshots
            payment_rows.append(
                {
                    "patient_id": int(pid),
                    "payment_mode": self._as_str(_pmode),
                    "payment_amount": self._to_float(_pamt),
                    "due_amount": self._to_float(row.get("due_amount")),
                    "extra_amount": self._to_float(row.get("extra_amount")),
                    "additional_discount_amount": patient_additional,
                    "payment_screenshot_paths": screenshots,
                    "total_amount": round(patient_total, 2),
                }
            )

        final_discount = base_discount + additional_discount
        total_amount = max(0.0, charge_total - additional_discount)
        existing_payment_snapshot = self._appointment_payment_snapshot_obj(existing_payment_snapshot_raw)
        patient_context = existing_payment_snapshot.get("patient_context") if isinstance(existing_payment_snapshot.get("patient_context"), dict) else {}
        return {
            "payments": payment_rows,
            "payment_screenshots": payment_screenshots,
            "summary": {
                "sub_total": round(sub_total, 2),
                "credit_amount": 0.0,
                "paying_amount": round(charge_total, 2),
                "base_discount": round(base_discount, 2),
                "additional_discount": round(additional_discount, 2),
                "final_discount": round(final_discount, 2),
                "total_amount": round(total_amount, 2),
            },
            "patient_context": patient_context,
        }


    @staticmethod
    def _split_patient_names(raw: object) -> list[str]:
        text = BookingService._as_str(raw)
        if not text:
            return []
        return [x.strip() for x in text.split(',') if x and x.strip()]

    def build_service_note_payload_by_id(
        self,
        booking_id: int | None = None,
        appointment_id: int | None = None,
    ) -> dict:
        if booking_id is None and appointment_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="booking_id or appointment_id is required",
            )

        appointment_row = None
        selected_patient_ids: list[int] = []
        appointment_payment_snapshot: dict = {}
        appointment_tests_snapshot_raw: str | None = None
        appointment_patient_context: dict = {}

        if appointment_id is not None:
            appointment_row = self.repository.db.execute(
                text(
                    """
                    SELECT
                        id,
                        booking_id,
                        preferred_visit_date,
                        preferred_time_slot,
                        selected_address_id,
                        address_snapshot_json,
                        selected_patient_ids_json,
                        appointment_tests_snapshot_json,
                        payment_snapshot_json,
                        assigned_phlebotomist_id
                    FROM hhome_collection_booking_appointment
                    WHERE id = :appointment_id
                    LIMIT 1
                    """
                ),
                {"appointment_id": int(appointment_id)},
            ).mappings().first()
            if not appointment_row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Appointment not found",
                )
            resolved_booking_id = int(appointment_row.get("booking_id") or 0)
            if booking_id is not None and int(booking_id) != resolved_booking_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Appointment does not belong to provided booking",
                )
            booking_id = resolved_booking_id
            selected_patient_ids = self.repository._parse_selected_patient_ids(
                appointment_row.get("selected_patient_ids_json")
            )
            appointment_tests_snapshot_raw = (
                str(appointment_row.get("appointment_tests_snapshot_json"))
                if appointment_row.get("appointment_tests_snapshot_json") is not None
                else None
            )
            appointment_payment_snapshot = self._appointment_payment_snapshot_obj(
                appointment_row.get("payment_snapshot_json")
            )
            appointment_patient_context = (
                appointment_payment_snapshot.get("patient_context")
                if isinstance(appointment_payment_snapshot.get("patient_context"), dict)
                else {}
            )

        booking = self.repository.get_booking_by_id(int(booking_id or 0))
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found",
            )

        address_id = (
            appointment_row.get("selected_address_id")
            if appointment_row and appointment_row.get("selected_address_id") is not None
            else booking.selected_address_id
        )
        address = self.repository.get_address(address_id) or {}
        address = self._merge_address_snapshot(
            address,
            (appointment_row.get("address_snapshot_json") if appointment_row else None) or getattr(booking, "address_snapshot_json", None),
        )

        patient_scope = selected_patient_ids if selected_patient_ids else None
        booking_patient_cols = self.repository._get_table_columns("hhome_collection_booking_patient")
        patient_master_cols = self.repository._get_table_columns("hpatient_master")
        patient_params: dict[str, object] = {"booking_id": int(booking.id)}
        patient_scope_sql = self.repository._build_patient_scope_where(
            patient_ids=patient_scope,
            params=patient_params,
            column_name="bp.patient_id",
        )

        def _bp(col: str, alias: str | None = None) -> str:
            out_alias = alias or col
            return f"bp.{col} AS {out_alias}" if col in booking_patient_cols else f"NULL AS {out_alias}"

        def _pm(col: str, alias: str | None = None) -> str:
            out_alias = alias or col
            return f"p.{col} AS {out_alias}" if col in patient_master_cols else f"NULL AS {out_alias}"

        patient_rows = self.repository.db.execute(
            text(
                f"""
                SELECT
                    bp.id AS booking_patient_id,
                    bp.patient_id AS patient_id,
                    {_bp("booking_patient_status")},
                    {_bp("cce_level_TBS", "test_booking_status")},
                    {_bp("selected_comp_cat_ids")},
                    {_bp("ref_by", "referred_by")},
                    {_bp("selected_charge_modes")},
                    {_bp("selected_panel_companies")},
                    {_bp("patient_final_amount")},
                    {_bp("payment_amount")},
                    {_bp("additional_discount_amount")},
                    {_bp("payment_mode")},
                    {_bp("due_amount")},
                    {_bp("extra_amount")},
                    {_bp("prescription_files")},
                    {_bp("patient_photo_files")},
                    {_bp("payment_screenshot_paths")},
                    {_bp("APK_TBS", "apk_tbs")},
                    {_bp("report_schedule")},
                    {_bp("report_delivery")},
                    {_bp("no_of_pricks")},
                    {_bp("sample_collection_is")},
                    {_pm("patient_code")},
                    {_pm("title")},
                    {_pm("full_name")},
                    {_pm("gender")},
                    {_pm("age_years")},
                    {_pm("date_of_birth")},
                    {_pm("contact_mobile")},
                    {_pm("alternate_mobile")},
                    {_pm("panel_company")},
                    {_pm("card_number", "card_no")},
                    {_pm("tag")},
                    {_pm("patient_documents")}
                FROM hhome_collection_booking_patient bp
                INNER JOIN hpatient_master p ON p.id = bp.patient_id
                WHERE bp.booking_id = :booking_id
                {patient_scope_sql}
                ORDER BY bp.id ASC, p.id ASC
                """
            ),
            patient_params,
        ).mappings().all()

        if appointment_id is not None and appointment_tests_snapshot_raw:
            tests_by_patient = self._build_tests_from_appointment_snapshot(
                appointment_tests_snapshot_raw,
                patient_ids=selected_patient_ids if selected_patient_ids else None,
            )
        else:
            tests_by_patient = self.repository.get_tests_for_booking(
                booking_id=int(booking.id),
                patient_ids=selected_patient_ids if selected_patient_ids else None,
                pending_only=False,
            )

        assigned_phlebo_id = (
            int(appointment_row.get("assigned_phlebotomist_id") or 0)
            if appointment_row
            else int(getattr(booking, "assigned_phlebotomist_id", 0) or 0)
        )
        phlebo = None
        if assigned_phlebo_id > 0:
            phlebo = self.repository.db.execute(
                text("SELECT id, name, contact FROM users WHERE id = :user_id LIMIT 1"),
                {"user_id": assigned_phlebo_id},
            ).mappings().first()

        tests_payload: list[dict] = []
        patient_updates: list[dict] = []
        sample_collection_pick_patients: list[dict] = []
        total_amount = self._to_float(getattr(booking, "total_amount", 0))
        if appointment_id is not None:
            summary = (
                appointment_payment_snapshot.get("summary")
                if isinstance(appointment_payment_snapshot.get("summary"), dict)
                else {}
            )
            total_amount = self._to_float(summary.get("total_amount"))

        for row in patient_rows:
            patient_id = int(row.get("patient_id") or 0)
            if patient_id <= 0:
                continue
            appointment_ctx = (
                appointment_patient_context.get(str(patient_id))
                if isinstance(appointment_patient_context.get(str(patient_id)), dict)
                else {}
            )
            patient_name = " ".join(
                x for x in [self._as_str(row.get("title")), self._as_str(row.get("full_name"))] if x
            ).strip() or f"Patient {patient_id}"

            prescription_files = self._split_csv_values(row.get("prescription_files"))
            patient_documents = self._split_csv_values(row.get("patient_documents"))
            patient_photo_files = self._split_csv_values(row.get("patient_photo_files"))
            payment_screenshot_paths = self._split_csv_values(row.get("payment_screenshot_paths"))
            apk_tbs = self._as_str(row.get("apk_tbs")) or self._as_str(row.get("test_booking_status"))
            manual_hc_slip_paths = payment_screenshot_paths if self._is_manual_hcb_slip(apk_tbs) else []

            patient_tests = tests_by_patient.get(patient_id, []) or []
            tests_payload.append(
                {
                    "patient_id": patient_id,
                    "test_booking_status": apk_tbs,
                    "report_schedule": self._as_str(row.get("report_schedule")),
                    "report_delivery_options": self._as_str(row.get("report_delivery")),
                    "panels": self._group_tests_into_panels(patient_tests, row),
                }
            )

            patient_final_amount = (
                self._to_float(appointment_ctx.get("booking_total_amount"))
                if appointment_id is not None and appointment_ctx.get("booking_total_amount") is not None
                else self._to_float(row.get("patient_final_amount"))
            )
            if patient_final_amount <= 0:
                patient_final_amount = round(sum(self._to_float(t.get("charge")) for t in patient_tests), 2)
            payment_amount = (
                self._to_float(appointment_ctx.get("payment_amount"))
                if appointment_id is not None and appointment_ctx.get("payment_amount") is not None
                else self._to_float(row.get("payment_amount"))
            )

            documents: list[dict] = []
            for value in prescription_files:
                documents.append(
                    {
                        "type": "prescription",
                        "file": value,
                        "url": self._public_upload_url(
                            value if str(value).strip().startswith("/static/uploads/") else f"/static/uploads/prescriptions/{str(value).strip().lstrip('/')}"
                        ),
                    }
                )
            for value in patient_documents:
                documents.append(
                    {
                        "type": "patient_document",
                        "file": value,
                        "url": self._public_upload_url(
                            value if str(value).strip().startswith("/static/uploads/") else f"/static/uploads/patient_documents/{str(value).strip().lstrip('/')}"
                        ),
                    }
                )

            patient_updates.append(
                {
                    "booking_code": self._as_str(getattr(booking, "booking_code", None)),
                    "patient_id": patient_id,
                    "booking_patient_status": int(row.get("booking_patient_status") or 0),
                    "booking_patient_id": int(row.get("booking_patient_id") or 0),
                    "patient_name": patient_name,
                    "referred_by": self._as_str(row.get("referred_by")),
                    "apk_tbs": apk_tbs,
                    "test_booking_status": self._as_str(row.get("test_booking_status")),
                    "report_schedule": self._as_str(row.get("report_schedule")),
                    "report_delivery": self._as_str(row.get("report_delivery")),
                    "no_of_pricks": self._as_str(row.get("no_of_pricks")),
                    "sample_collection_is": self._as_str(row.get("sample_collection_is")),
                    "payment_mode": self._as_str(appointment_ctx.get("booking_payment_mode")) if appointment_id is not None else self._as_str(row.get("payment_mode")),
                    "due_amount": self._to_float(appointment_ctx.get("booking_due_amount") if appointment_id is not None else row.get("due_amount")),
                    "extra_amount": self._to_float(appointment_ctx.get("booking_extra_amount") if appointment_id is not None else row.get("extra_amount")),
                    "additional_discount_amount": self._to_float(
                        appointment_ctx.get("appointment_additional_discount_amount")
                        if appointment_id is not None
                        else row.get("additional_discount_amount")
                    ),
                    "payment_amount": payment_amount,
                    "patient_final_amount": patient_final_amount,
                    "prescription_files": prescription_files,
                    "patient_documents": patient_documents,
                    "patient_photo_files": patient_photo_files,
                    "payment_screenshot_paths": payment_screenshot_paths,
                    "manual_hc_slip_paths": manual_hc_slip_paths,
                    "documents": documents,
                }
            )

            sample_collection_pick_patients.append(
                {
                    "id": patient_id,
                    "patient_id": patient_id,
                    "sourcePatientId": patient_id,
                    "booking_patient_status": int(row.get("booking_patient_status") or 0),
                    "name": patient_name,
                    "title": self._as_str(row.get("title")),
                    "full_name": self._as_str(row.get("full_name")),
                    "gender": self._as_str(row.get("gender")),
                    "age_years": int(row.get("age_years")) if row.get("age_years") is not None else None,
                    "date_of_birth": self._safe_date(row.get("date_of_birth")).isoformat() if self._safe_date(row.get("date_of_birth")) else None,
                    "contact_mobile": self._as_str(row.get("contact_mobile")),
                    "alternate_mobile": self._as_str(row.get("alternate_mobile")),
                    "panel_company": self._as_str(row.get("panel_company")),
                    "card_no": self._as_str(row.get("card_no")),
                    "tag": self._as_str(row.get("tag")),
                }
            )

        booking_date_value = (
            appointment_row.get("preferred_visit_date")
            if appointment_row and appointment_row.get("preferred_visit_date") is not None
            else getattr(booking, "preferred_visit_date", None)
        )
        booking_date = booking_date_value.isoformat() if hasattr(booking_date_value, "isoformat") else self._as_str(booking_date_value)
        time_slot = (
            self._as_str(appointment_row.get("preferred_time_slot"))
            if appointment_row
            else self._as_str(getattr(booking, "preferred_time_slot", None))
        )

        payload = {
            "booking_id": self._as_str(getattr(booking, "booking_code", None)) or int(booking.id),
            "booking_code": self._as_str(getattr(booking, "booking_code", None)),
            "id": int(booking.id),
            "appointment_id": int(appointment_id) if appointment_id is not None else None,
            "source_type": "APPOINTMENT" if appointment_id is not None else "BOOKING",
            "patient_scope": "APPOINTMENT_SELECTED" if selected_patient_ids else "BOOKING_ALL_FALLBACK",
            "booking_date": booking_date,
            "time_slot": time_slot,
            "address": address,
            "total_amount": total_amount,
            "referred_by": self._as_str(getattr(booking, "referred_by", None)),
            "intrnl_rfrncd_by": self._as_str(getattr(booking, "intrnl_rfrncd_by", None)),
            "phlebo_name": self._as_str(phlebo.get("name")) if phlebo else None,
            "phlebo_mobile": self._as_str(phlebo.get("contact")) if phlebo else None,
            "patient_updates": patient_updates,
            "tests_payload": tests_payload,
            "sample_collection_pick_patients": sample_collection_pick_patients,
        }
        return payload

    def get_my_assigned_bookings(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BookingSummary]:
        started_at = perf_counter()
        rows = self.repository.get_my_assigned_merged(
            user_id=user_id,
            status_filter=[0, 1, 2],
            include_terminal=False,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        result = [
            BookingSummary(
                id=row["id"],
                booking_status=row.get("booking_status"),
                preferred_visit_date=row.get("preferred_visit_date"),
                preferred_time_slot=self._as_str(row.get("preferred_time_slot")),
                source_type=row.get("source_type", "BOOKING"),
                patient_scope=row.get("patient_scope", "BOOKING_ALL_FALLBACK"),
                booking_id=row.get("booking_id"),
                appointment_id=row.get("appointment_id"),
                appointment_no=self._as_str(row.get("appointment_no")),
                caller_mobile=self._as_str(row.get("caller_mobile")),
                route=self._as_str(row.get("route")),
                patient_names=self._split_patient_names(row.get("patient_names")),
                tag=int(row.get("tag") or 0),
            )
            for row in rows
        ]
        self._logger.info(
            "assigned_list_timing user_id=%s count=%s limit=%s offset=%s duration_ms=%.2f",
            user_id,
            len(result),
            limit,
            offset,
            (perf_counter() - started_at) * 1000,
        )
        return result

    def get_my_assigned_history_bookings(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BookingSummary]:
        started_at = perf_counter()
        rows = self.repository.get_my_assigned_merged(
            user_id=user_id,
            status_filter=[3, 4, 5],
            include_terminal=True,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        result = [
            BookingSummary(
                id=row["id"],
                booking_status=row.get("booking_status"),
                preferred_visit_date=row.get("preferred_visit_date"),
                preferred_time_slot=self._as_str(row.get("preferred_time_slot")),
                source_type=row.get("source_type", "BOOKING"),
                patient_scope=row.get("patient_scope", "BOOKING_ALL_FALLBACK"),
                booking_id=row.get("booking_id"),
                appointment_id=row.get("appointment_id"),
                appointment_no=self._as_str(row.get("appointment_no")),
                caller_mobile=self._as_str(row.get("caller_mobile")),
                route=self._as_str(row.get("route")),
                patient_names=self._split_patient_names(row.get("patient_names")),
            )
            for row in rows
        ]
        self._logger.info(
            "assigned_history_timing user_id=%s count=%s limit=%s offset=%s duration_ms=%.2f",
            user_id,
            len(result),
            limit,
            offset,
            (perf_counter() - started_at) * 1000,
        )
        return result

    def get_my_assigned_booking_details(
        self,
        booking_id: int,
        user_id: int,
        exclude_cancelled: bool = True,
        appointment_id: int | None = None,
        catalog_db: Session | None = None,
    ) -> BookingDetailsResponse:
        started_at = perf_counter()
        if appointment_id is not None:
            booking = self.repository.get_booking_by_id(booking_id=booking_id)
        else:
            booking = self.repository.get_assigned_booking_by_id(
                booking_id=booking_id,
                user_id=user_id,
                exclude_cancelled=exclude_cancelled,
            )
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found or not assigned to current user",
            )

        address = self.repository.get_address(booking.selected_address_id)
        patient_scope = "BOOKING_ALL_FALLBACK"
        selected_patient_ids: list[int] = []
        status_for_response = int(booking.booking_status) if booking.booking_status is not None else None
        appointment_payment_snapshot = {}
        appointment_patient_context = {}
        if appointment_id is not None:
            selected_booking_id, appointment_status, selected_patient_ids, patient_scope = self.repository.get_appointment_selected_patient_ids(
                appointment_id=appointment_id,
                user_id=user_id,
            )
            if selected_booking_id is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Appointment not found or not assigned to current user",
                )
            if int(selected_booking_id) != int(booking.id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Appointment does not belong to provided booking",
                )
            status_for_response = appointment_status if appointment_status is not None else status_for_response
            appt_snapshot_raw = self.repository.get_appointment_tests_snapshot(
                appointment_id=appointment_id,
                user_id=user_id,
            )
            appointment_payment_snapshot = self._appointment_payment_snapshot_obj(
                self.repository.db.execute(
                    text("SELECT payment_snapshot_json FROM hhome_collection_booking_appointment WHERE id=:appointment_id LIMIT 1"),
                    {"appointment_id": int(appointment_id)},
                ).scalar()
            )
            appointment_patient_context = appointment_payment_snapshot.get("patient_context") if isinstance(appointment_payment_snapshot.get("patient_context"), dict) else {}

        patients = self.repository.get_patients_for_booking(
            booking.id,
            patient_ids=selected_patient_ids if selected_patient_ids else None,
        )
        panel_identity_by_name: dict[str, dict[str, str | None]] = {}
        if catalog_db:
            panel_names = sorted(
                {
                    self._as_str(patient.panel_company)
                    for *_, patient in patients
                    if self._as_str(patient.panel_company)
                }
            )
            if panel_names:
                stmt = text(
                    """
                    SELECT pname, code, ABARID
                    FROM address
                    WHERE pname IN :panel_names
                    """
                ).bindparams(bindparam("panel_names", expanding=True))
                for row in catalog_db.execute(stmt, {"panel_names": panel_names}).mappings():
                    pname = self._as_str(row.get("pname"))
                    if not pname:
                        continue
                    # First exact match wins to keep response stable.
                    panel_identity_by_name.setdefault(
                        pname,
                        {
                            "panel_code": self._as_str(row.get("code")),
                            "panel_abarid": self._as_str(row.get("ABARID")),
                        },
                    )
        pending_only_tests = status_for_response in {0, 1, 2}
        tests_by_patient: dict[int, list[dict]] = {}
        if appointment_id is not None:
            tests_by_patient = self._build_tests_from_appointment_snapshot(
                appt_snapshot_raw,
                patient_ids=selected_patient_ids if selected_patient_ids else None,
            )
        if not tests_by_patient:
            tests_by_patient = self.repository.get_tests_for_booking(
                booking.id,
                patient_ids=selected_patient_ids if selected_patient_ids else None,
                pending_only=pending_only_tests,
            )

        patient_items = []
        booking_patient_ids = set()
        for (
            booking_patient_id,
            booking_patient_status,
            test_booking_status,
            selected_comp_cat_ids,
            patient_referred_by,
            selected_charge_modes,
            selected_panel_companies,
            additional_discount_amount,
            payment_mode,
            due_amount,
            extra_amount,
            bp_prescription_files,
            patient,
        ) in patients:
            booking_patient_ids.add(int(patient.id))
            identity = panel_identity_by_name.get(self._as_str(patient.panel_company) or "")
            patient_document_files = self._split_csv_values(getattr(patient, "patient_documents", None))
            prescription_files = self._split_csv_values(bp_prescription_files)
            patient_documents = []
            patient_document_urls = []
            for name in patient_document_files:
                n = str(name)
                u = n.upper()
                if "_CGHS_" in u:
                    dtype = "cghs_card"
                else:
                    dtype = "patient_document"
                url = self._public_upload_url(f"/static/uploads/patient_documents/{n}")
                patient_documents.append({"file": n, "type": dtype, "url": url})
                patient_document_urls.append(url)
            prescription_urls = [
                self._public_upload_url(f"/static/uploads/prescriptions/{name}")
                for name in prescription_files
            ]
            patient_ctx = appointment_patient_context.get(str(int(patient.id))) if appointment_id is not None else None
            patient_ctx = patient_ctx if isinstance(patient_ctx, dict) else {}
            patient_items.append(
                PatientDetails(
                    id=patient.id,
                    booking_patient_id=int(booking_patient_id),
                    booking_patient_status=(None if appointment_id is not None else int(booking_patient_status or 0)),
                    test_booking_status=self._as_str(test_booking_status),
                    title=patient.title,
                    full_name=patient.full_name,
                    gender=patient.gender,
                    age_years=patient.age_years,
                    date_of_birth=self._safe_date(patient.date_of_birth),
                    contact_mobile=patient.contact_mobile,
                    alternate_mobile=patient.alternate_mobile,
                    panel_company=patient.panel_company,
                    card_no=self._as_str(getattr(patient, "card_no", None)),
                    panel_code=identity.get("panel_code") if identity else None,
                    panel_abarid=identity.get("panel_abarid") if identity else None,
                    selected_comp_cat_ids=self._as_str(selected_comp_cat_ids),
                    referred_by=self._as_str(patient_referred_by),
                    selected_charge_modes=self._as_str(selected_charge_modes),
                    selected_panel_companies=self._as_str(selected_panel_companies),
                    additional_discount_amount=(self._to_float(patient_ctx.get("appointment_additional_discount_amount")) if appointment_id is not None else self._to_float(additional_discount_amount)),
                    appointment_patient_status=(int(patient_ctx.get("appointment_patient_status")) if patient_ctx.get("appointment_patient_status") is not None else None),
                    booking_due_amount=self._to_float(patient_ctx.get("booking_due_amount") if appointment_id is not None else due_amount),
                    booking_extra_amount=self._to_float(patient_ctx.get("booking_extra_amount") if appointment_id is not None else extra_amount),
                    booking_payment_mode=(self._as_str(patient_ctx.get("booking_payment_mode")) if appointment_id is not None else self._as_str(payment_mode)),
                    tag=self._merge_csv_values(patient.tag, getattr(booking, "booking_tags", None)),
                    patient_documents=patient_documents,
                    patient_document_urls=patient_document_urls,
                    prescription_files=prescription_files,
                    prescription_urls=prescription_urls,
                    tests=tests_by_patient.get(patient.id, []),
                )
            )

        linked_patient_items = []
        if getattr(booking, "caller_id", None):
            linked_rows = self.repository.get_linked_patients_for_caller(
                caller_id=int(booking.caller_id),
                exclude_patient_ids=sorted(booking_patient_ids),
            )
            for lp in (linked_rows or []):
                linked_patient_items.append(
                    LinkedPatientDetails(
                        id=int(lp.get("id")),
                        patient_code=self._as_str(lp.get("patient_code")),
                        title=self._as_str(lp.get("title")),
                        full_name=self._as_str(lp.get("full_name")),
                        gender=self._as_str(lp.get("gender")),
                        age_years=int(lp.get("age_years")) if lp.get("age_years") is not None else None,
                        date_of_birth=self._safe_date(lp.get("date_of_birth")),
                        contact_mobile=self._as_str(lp.get("contact_mobile")),
                        alternate_mobile=self._as_str(lp.get("alternate_mobile")),
                        panel_company=self._as_str(lp.get("panel_company")),
                        tag=self._as_str(lp.get("tag")),
                    )
                )

        response_F_Apt_Am = float(getattr(booking, 'F_Apt_Am', 0) or 0)
        response_F_dis = float(getattr(booking, 'F_dis', 0) or 0)
        response_Ad_Dis = float(getattr(booking, 'Ad_Dis', 0) or 0)
        response_total_amount = float(getattr(booking, 'total_amount', 0) or 0)
        if appointment_id is not None:
            summary = appointment_payment_snapshot.get("summary") if isinstance(appointment_payment_snapshot.get("summary"), dict) else {}
            response_F_Apt_Am = self._to_float(summary.get("sub_total"))
            response_F_dis = self._to_float(summary.get("final_discount"))
            response_Ad_Dis = self._to_float(summary.get("additional_discount"))
            response_total_amount = self._to_float(summary.get("total_amount"))

        response = BookingDetailsResponse(
            booking_status=status_for_response,
            source_type="APPOINTMENT" if appointment_id is not None else "BOOKING",
            appointment_id=int(appointment_id) if appointment_id is not None else None,
            patient_scope=patient_scope,
            address=AddressDetails.model_validate(address) if address else None,
            patients=patient_items,
            linked_patients=linked_patient_items,
            F_Apt_Am=response_F_Apt_Am,
            F_dis=response_F_dis,
            Ad_Dis=response_Ad_Dis,
            total_amount=response_total_amount,
            intrnl_rfrncd_by=self._as_str(getattr(booking, 'intrnl_rfrncd_by', None)),
        )
        self._logger.info(
            "assigned_detail_timing booking_id=%s appointment_id=%s user_id=%s patients=%s duration_ms=%.2f",
            booking_id,
            appointment_id,
            user_id,
            len(patient_items),
            (perf_counter() - started_at) * 1000,
        )
        return response

    def update_assigned_booking_status(
        self,
        booking_id: int,
        user_id: int,
        action: str,
        appointment_id: int | None = None,
        payload=None,
        catalog_db: Session | None = None,
        patient_documents_map: dict[int, list] | None = None,
        payment_screenshots_map: dict[int, list] | None = None,
    ) -> BookingStatusUpdateResponse:
        request_started_at = perf_counter()
        if appointment_id is not None:
            booking = self.repository.get_booking_by_id(booking_id=booking_id)
        else:
            booking = self.repository.get_assigned_booking_by_id(
                booking_id=booking_id,
                user_id=user_id,
                exclude_cancelled=False,
            )
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found or not assigned to current user",
            )

        normalized_action = "complete" if action == "completed" else action
        if normalized_action == "cancel":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cancel action is not allowed on /status. Use /my-assigned/{booking_id}/cancel endpoint",
            )
        completion_lock_acquired = False
        lock_wait_ms = 0.0
        tests_phase_ms = 0.0
        uploads_phase_started_at = 0.0

        try:
            if normalized_action == "complete" and appointment_id is None:
                lock_wait_started_at = perf_counter()
                completion_lock_acquired = self.repository.acquire_booking_completion_lock(
                    booking.id,
                    wait_timeout_sec=self._settings.booking_completion_lock_wait_sec,
                )
                lock_wait_ms = (perf_counter() - lock_wait_started_at) * 1000
                if not completion_lock_acquired:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="This booking completion is already in progress. Please retry shortly",
                    )

            if normalized_action == "complete" and payload is not None:
                appointment_payment_screenshot_paths: dict[int, list[str]] = {}
                incoming_tests = getattr(payload, "tests_payload", None)
                if incoming_tests and appointment_id is None:
                    tests_phase_started_at = perf_counter()
                    save_payload = MobileBookingTestsSaveRequest.model_validate({
                        "additional_discount_mode": getattr(payload, "additional_discount_mode", None),
                        "additional_discount_value": getattr(payload, "additional_discount_value", 0) or 0,
                        "tests_payload": incoming_tests,
                    })
                    self.save_assigned_booking_tests(
                        booking_id=booking_id,
                        user_id=user_id,
                        payload=save_payload,
                        catalog_db=catalog_db,
                    )
                    tests_phase_ms = (perf_counter() - tests_phase_started_at) * 1000
                if incoming_tests and appointment_id is not None:
                    tests_billing_map: dict[str, dict] = {}
                    for pnode in (incoming_tests or []):
                        if not isinstance(pnode, dict):
                            continue
                        try:
                            _pid = int(pnode.get("patient_id") or 0)
                        except Exception:
                            _pid = 0
                        if _pid <= 0:
                            continue
                        k = str(_pid)
                        _tbs = pnode.get("test_booking_status")
                        tnode = {
                            "panel": {"pname": ""},
                            "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                            "selected_tests": [],
                            "panels": [],
                            "cce_level_tbs": _tbs,
                        }
                        for sec in (pnode.get("panels") or []):
                            if not isinstance(sec, dict):
                                continue
                            panel_name = str(sec.get("panel_company") or "").strip()
                            comp_cat_id = str(sec.get("comp_cat_id") or "").strip()
                            sel_mode = str(sec.get("selected_charge_mode") or "").strip()
                            selected_tests = []
                            for t in (sec.get("selected_tests") or []):
                                if not isinstance(t, dict):
                                    continue
                                code = str(t.get("booked_code") or "").strip()
                                if not code:
                                    continue
                                selected_tests.append({
                                    "booked_code": code,
                                    "description": str(t.get("description") or code).strip() or code,
                                    "charge": float(t.get("charge") or 0),
                                    "mrp": float(t.get("mrp") or 0),
                                    "max_discount": float(t.get("max_discount") or 0),
                                    "max_allowed_discount": float(t.get("max_allowed_discount") or 0),
                                })
                            tnode["panels"].append({
                                "panel": {"pname": panel_name},
                                "billing": {"comp_cat_id": comp_cat_id, "selected_charge_mode": sel_mode},
                                "selected_tests": selected_tests,
                            })
                            tnode["selected_tests"].extend(selected_tests)

                        tests_billing_map[k] = tnode

                    pending_tests_map: dict[str, dict] = {}
                    parent_context_map: dict[str, dict] = {}
                    for prow in (getattr(payload, "pending_child_tests", None) or []):
                        r = prow or {}
                        try:
                            _pid = int(r.get("patient_id") or 0)
                        except Exception:
                            _pid = 0
                        if _pid <= 0:
                            continue
                        key = str(_pid)
                        root_code = str(r.get("root_booked_code") or "").strip()
                        root_name = str(r.get("root_test_name") or root_code).strip() or root_code
                        if root_code:
                            parent_context_map.setdefault(key, {
                                "panel": {"pname": ""},
                                "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                "selected_tests": [],
                                "panels": [{"panel": {"pname": ""}, "billing": {"comp_cat_id": "", "selected_charge_mode": ""}, "selected_tests": []}],
                                "cce_level_tbs": None,
                            })
                            _pr = {"booked_code": root_code, "description": root_name, "charge": 0, "mrp": 0, "max_discount": 0, "max_allowed_discount": 0}
                            parent_context_map[key]["selected_tests"].append(_pr)
                            parent_context_map[key]["panels"][0]["selected_tests"].append(_pr)

                        child_rows = []
                        for p in (r.get("pending") or r.get("pending_child_tests") or []):
                            item = p or {}
                            code = str(item.get("booked_code") or "").strip()
                            if not code:
                                continue
                            child_rows.append({
                                "booked_code": code,
                                "parent_booked_code": str(item.get("parent_booked_code") or root_code).strip() or None,
                                "description": str(item.get("description") or code).strip() or code,
                                "charge": 0,
                                "mrp": 0,
                                "max_discount": 0,
                                "max_allowed_discount": 0,
                            })
                        if child_rows:
                            pending_tests_map.setdefault(key, {
                                "panel": {"pname": ""},
                                "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                "selected_tests": [],
                                "panels": [{"panel": {"pname": ""}, "billing": {"comp_cat_id": "", "selected_charge_mode": ""}, "selected_tests": []}],
                                "cce_level_tbs": None,
                            })
                            pending_tests_map[key]["selected_tests"].extend(child_rows)
                            pending_tests_map[key]["panels"][0]["selected_tests"].extend(child_rows)

                    appointment_snapshot_payload = {
                        "tests_billing_map": tests_billing_map,
                        "pending_tests_map": pending_tests_map,
                        "parent_context_map": parent_context_map,
                        "flow_type": "appointment_complete_payload",
                    }
                    self.repository.save_appointment_tests_snapshot(
                        booking_id=int(booking.id),
                        appointment_id=int(appointment_id),
                        user_id=int(user_id),
                        snapshot_payload=appointment_snapshot_payload,
                    )
                # Save only deselected/pending child tests for this booking context.
                _pending_rows = getattr(payload, "pending_child_tests", None) or []
                if appointment_id is not None and bool(getattr(payload, "followup_required", False)) and not _pending_rows:
                    _pending_rows = self._appointment_pending_rows_fallback(
                        booking_id=int(booking.id),
                        appointment_id=int(appointment_id),
                        user_id=int(user_id),
                    )
                _patient_scope_ids = []
                for _u in (getattr(payload, "patient_updates", None) or []):
                    try:
                        _pid = int((_u or {}).get("patient_id") or 0)
                    except Exception:
                        _pid = 0
                    if _pid > 0:
                        _patient_scope_ids.append(_pid)
                self.repository.replace_pending_child_tests_for_booking(
                    booking_id=booking.id,
                    pending_rows=_pending_rows,
                    source_type=("APPOINTMENT" if appointment_id is not None else "BOOKING"),
                    appointment_id=(int(appointment_id) if appointment_id is not None else None),
                    actor_user_id=user_id,
                    patient_ids_scope=_patient_scope_ids,
                )
                # Persist patient-level completion fields (APK_TBS/report/payment/pricks/sample collection/cancel metadata).
                patient_updates = getattr(payload, "patient_updates", None) or []
                if patient_updates:
                    self.repository.apply_completion_patient_updates(
                        booking_id=booking.id,
                        updates=patient_updates,
                        actor_user_id=user_id,
                        include_payment_fields=(appointment_id is None),
                        recompute_booking_totals=(appointment_id is None),
                        include_cmplt_tube_field=(appointment_id is None),
                    )
                    if appointment_id is not None:
                        self.repository.save_appointment_completed_tubes(
                            booking_id=int(booking.id),
                            appointment_id=int(appointment_id),
                            updates=patient_updates,
                        )
                    self.repository.handle_cancelled_patient_reschedule(
                        booking_id=booking.id,
                        updates=patient_updates,
                        actor_user_id=user_id,
                    )

                if patient_documents_map:
                    if uploads_phase_started_at == 0.0:
                        uploads_phase_started_at = perf_counter()
                    booking_code = str(getattr(booking, "booking_code", "") or "").strip()
                    patient_updates_by_id: dict[int, dict] = {}
                    for row in (patient_updates or []):
                        if not isinstance(row, dict):
                            continue
                        try:
                            rid = int(row.get("patient_id") or 0)
                        except Exception:
                            rid = 0
                        if rid > 0:
                            patient_updates_by_id[rid] = row
                    for patient_id_raw, files_raw in (patient_documents_map or {}).items():
                        try:
                            patient_id = int(patient_id_raw)
                        except Exception:
                            continue
                        files = [f for f in (files_raw or []) if f is not None]
                        if patient_id <= 0 or not files:
                            continue
                        context = self.repository.get_booking_patient_context(
                            booking_id=booking.id,
                            patient_id=patient_id,
                        )
                        if not context:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Patient {patient_id} is not mapped to booking {booking.id}",
                            )
                        update_row = patient_updates_by_id.get(patient_id) or {}
                        declared_docs = update_row.get("documents") if isinstance(update_row, dict) else []
                        doc_types: list[str] = []
                        expected_field = f"patient_documents_{patient_id}"
                        if isinstance(declared_docs, list):
                            for d in declared_docs:
                                if not isinstance(d, dict):
                                    continue
                                if str(d.get("file_field") or "").strip() != expected_field:
                                    continue
                                dtype = str(d.get("type") or "").strip().lower()
                                if dtype:
                                    doc_types.append(dtype)

                        prescription_files: list = []
                        patient_photo_files: list = []
                        patient_doc_files: list = []
                        patient_doc_types: list[str] = []
                        manual_hcb_files: list = []
                        for idx, f in enumerate(files):
                            dtype = doc_types[idx] if idx < len(doc_types) else ""
                            if dtype == "cghs_card":
                                patient_doc_files.append(f)
                                patient_doc_types.append(dtype)
                            elif dtype == "patient_photo":
                                patient_photo_files.append(f)
                            elif dtype == "prescription":
                                prescription_files.append(f)
                            elif self._is_manual_hcb_slip(dtype):
                                manual_hcb_files.append(f)
                            else:
                                # Backward compatible fallback for legacy payloads.
                                prescription_files.append(f)

                        existing_prescriptions = self.repository.get_patient_prescription_paths(booking_id=booking.id, patient_id=patient_id)
                        existing_patient_docs = self.repository.get_patient_document_paths(patient_id=patient_id)
                        existing_patient_photos = self.repository.get_patient_photo_paths(booking_id=booking.id, patient_id=patient_id)
                        saved_abs_paths: list[Path] = []
                        try:
                            if prescription_files:
                                prescription_paths = self._save_booking_prescriptions(
                                    booking_code=booking_code,
                                    patient_id=patient_id,
                                    files=prescription_files,
                                    existing_prescriptions=existing_prescriptions,
                                    saved_abs_paths=saved_abs_paths,
                                )
                                self.repository.update_patient_prescription_files(
                                    booking_id=booking.id,
                                    patient_id=patient_id,
                                    files=prescription_paths,
                                )
                            if patient_photo_files:
                                patient_photo_paths = self._save_booking_patient_photos(
                                    booking_code=booking_code,
                                    patient_id=patient_id,
                                    files=patient_photo_files,
                                    existing_photos=existing_patient_photos,
                                    saved_abs_paths=saved_abs_paths,
                                )
                                self.repository.update_patient_photo_files(
                                    booking_id=booking.id,
                                    patient_id=patient_id,
                                    files=patient_photo_paths,
                                )
                            if patient_doc_files:
                                patient_doc_paths = self._save_patient_documents(
                                    patient_id=patient_id,
                                    files=patient_doc_files,
                                    existing_documents=existing_patient_docs,
                                    saved_abs_paths=saved_abs_paths,
                                    file_types=patient_doc_types,
                                )
                                self.repository.update_patient_documents(
                                    patient_id=patient_id,
                                    documents=patient_doc_paths,
                                )
                            if manual_hcb_files:
                                hcb_paths = self._save_hc_slip_files(
                                    booking_code=booking_code,
                                    patient_id=patient_id,
                                    files=manual_hcb_files,
                                )
                                if hcb_paths:
                                    self.repository.set_patient_payment_screenshots(
                                        booking_id=booking.id,
                                        patient_id=patient_id,
                                        rel_paths=hcb_paths,
                                    )
                        except Exception:
                            self._cleanup_saved_files(saved_abs_paths)
                            raise

                if payment_screenshots_map:
                    if uploads_phase_started_at == 0.0:
                        uploads_phase_started_at = perf_counter()
                    booking_code = str(getattr(booking, "booking_code", "") or "").strip()
                    patient_updates_by_id: dict[int, dict] = {}
                    for row in (patient_updates or []):
                        if not isinstance(row, dict):
                            continue
                        try:
                            rid = int(row.get("patient_id") or 0)
                        except Exception:
                            rid = 0
                        if rid > 0:
                            patient_updates_by_id[rid] = row
                    for patient_id_raw, files_raw in (payment_screenshots_map or {}).items():
                        try:
                            patient_id = int(patient_id_raw)
                        except Exception:
                            continue
                        files = [f for f in (files_raw or []) if f is not None]
                        if patient_id <= 0 or not files:
                            continue
                        prow = patient_updates_by_id.get(patient_id) or {}
                        _ = prow
                        paths = self._save_payment_screenshots(
                            booking_code=booking_code,
                            patient_id=patient_id,
                            files=files,
                            category=None,
                            name_mode="pay",
                        )
                        if paths:
                            if appointment_id is not None:
                                appointment_payment_screenshot_paths[int(patient_id)] = paths
                            else:
                                self.repository.set_patient_payment_screenshots(
                                    booking_id=booking.id,
                                    patient_id=patient_id,
                                    rel_paths=paths,
                                )

                followup_required = bool(getattr(payload, "followup_required", False))
                pending_child_rows = (_pending_rows or [])
                if followup_required and pending_child_rows:
                    selected_ids = sorted({
                        int((row or {}).get("patient_id") or 0)
                        for row in pending_child_rows
                        if int((row or {}).get("patient_id") or 0) > 0
                    })
                    tbs_by_pid: dict[int, object] = {}
                    if selected_ids:
                        tbs_rows = self.repository.db.execute(
                            text(
                                """
                                SELECT patient_id, cce_level_TBS
                                FROM hhome_collection_booking_patient
                                WHERE booking_id=:bid
                                  AND patient_id IN :pids
                                """
                            ).bindparams(bindparam("pids", expanding=True)),
                            {"bid": int(booking.id), "pids": selected_ids},
                        ).mappings().all()
                        for rr in tbs_rows:
                            pid = int(rr.get("patient_id") or 0)
                            if pid > 0:
                                tbs_by_pid[pid] = rr.get("cce_level_TBS")

                    tests_billing_map: dict[str, dict] = {}
                    pending_tests_map: dict[str, dict] = {}
                    parent_context_map: dict[str, dict] = {}
                    for row in pending_child_rows:
                        r = row or {}
                        pid = int(r.get("patient_id") or 0)
                        if pid <= 0:
                            continue
                        key = str(pid)
                        tbs_value = tbs_by_pid.get(pid)
                        root_code = str(r.get("root_booked_code") or "").strip()
                        pending_items = r.get("pending") or r.get("pending_child_tests") or []
                        child_rows = []
                        for p in pending_items:
                            item = p or {}
                            code = str(item.get("booked_code") or "").strip()
                            if not code:
                                continue
                            child_rows.append({
                                "booked_code": code,
                                "parent_booked_code": str(item.get("parent_booked_code") or root_code).strip() or None,
                                "description": str(item.get("description") or code).strip() or code,
                                "charge": 0,
                                "mrp": 0,
                                "max_discount": 0,
                                "max_allowed_discount": 0,
                            })

                        tests_billing_map.setdefault(key, {
                            "panel": {"pname": ""},
                            "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                            "selected_tests": [],
                            "panels": [{
                                "panel": {"pname": ""},
                                "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                "selected_tests": [],
                            }],
                            "cce_level_tbs": tbs_value,
                        })
                        if tests_billing_map[key].get("cce_level_tbs") in (None, "") and tbs_value not in (None, ""):
                            tests_billing_map[key]["cce_level_tbs"] = tbs_value

                        if root_code:
                            root_name = str(r.get("root_test_name") or root_code).strip() or root_code
                            root_row = {
                                "booked_code": root_code,
                                "description": root_name,
                                "charge": 0,
                                "mrp": 0,
                                "max_discount": 0,
                                "max_allowed_discount": 0,
                            }
                            parent_context_map.setdefault(key, {
                                "panel": {"pname": ""},
                                "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                "selected_tests": [],
                                "panels": [{
                                    "panel": {"pname": ""},
                                    "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                    "selected_tests": [],
                                }],
                                "cce_level_tbs": tbs_value,
                            })
                            if parent_context_map[key].get("cce_level_tbs") in (None, "") and tbs_value not in (None, ""):
                                parent_context_map[key]["cce_level_tbs"] = tbs_value
                            existing_parent = {str(x.get("booked_code") or "").strip().upper() for x in (parent_context_map[key].get("selected_tests") or [])}
                            if root_code.strip().upper() not in existing_parent:
                                parent_context_map[key]["selected_tests"].append(dict(root_row))
                                parent_context_map[key]["panels"][0]["selected_tests"].append(dict(root_row))
                            existing_root = {str(x.get("booked_code") or "").strip().upper() for x in (tests_billing_map[key].get("selected_tests") or [])}
                            if root_code.strip().upper() not in existing_root:
                                tests_billing_map[key]["selected_tests"].append(dict(root_row))
                                tests_billing_map[key]["panels"][0]["selected_tests"].append(dict(root_row))

                        if child_rows:
                            pending_tests_map.setdefault(key, {
                                "panel": {"pname": ""},
                                "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                "selected_tests": [],
                                "panels": [{
                                    "panel": {"pname": ""},
                                    "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                    "selected_tests": [],
                                }],
                                "cce_level_tbs": tbs_value,
                            })
                            if pending_tests_map[key].get("cce_level_tbs") in (None, "") and tbs_value not in (None, ""):
                                pending_tests_map[key]["cce_level_tbs"] = tbs_value

                            existing_child = {str(x.get("booked_code") or "").strip().upper() for x in (pending_tests_map[key].get("selected_tests") or [])}
                            for child in child_rows:
                                ccode = str(child.get("booked_code") or "").strip().upper()
                                if not ccode or ccode in existing_child:
                                    continue
                                pending_tests_map[key]["selected_tests"].append(child)
                                pending_tests_map[key]["panels"][0]["selected_tests"].append(child)
                                existing_child.add(ccode)

                    snapshot_payload = {
                        "tests_billing_map": tests_billing_map,
                        "pending_tests_map": pending_tests_map,
                        "parent_context_map": parent_context_map,
                        "flow_type": "auto_followup_pending_child",
                    }

                    self.repository.create_auto_followup_appointment(
                        booking_id=booking.id,
                        actor_user_id=int(getattr(payload, "followup_created_by", None) or user_id),
                        preferred_date=getattr(payload, "followup_date", None),
                        preferred_slot=getattr(payload, "followup_time_slot", None),
                        selected_patient_ids=selected_ids,
                        appointment_tests_snapshot=snapshot_payload,
                    )
                if appointment_id is not None:
                    appt_snapshot = self.repository.get_appointment_tests_snapshot(
                        appointment_id=int(appointment_id),
                        user_id=int(user_id),
                    )
                    existing_payment_snapshot_raw = self.repository.db.execute(
                        text("SELECT payment_snapshot_json FROM hhome_collection_booking_appointment WHERE id=:appointment_id LIMIT 1"),
                        {"appointment_id": int(appointment_id)},
                    ).scalar()
                    _selected_booking_id, _appointment_status, selected_ids, _scope = (
                        self.repository.get_appointment_selected_patient_ids(
                            appointment_id=int(appointment_id),
                            user_id=int(user_id),
                        )
                    )
                    payment_snapshot = self._build_appointment_payment_snapshot(
                        appointment_snapshot_raw=appt_snapshot,
                        patient_updates=patient_updates,
                        payment_screenshot_paths_by_patient=appointment_payment_screenshot_paths,
                        patient_ids=selected_ids or None,
                        existing_payment_snapshot_raw=(str(existing_payment_snapshot_raw) if existing_payment_snapshot_raw is not None else None),
                    )
                    self.repository.save_appointment_payment_snapshot(
                        booking_id=int(booking.id),
                        appointment_id=int(appointment_id),
                        snapshot_payload=payment_snapshot,
                    )
            if appointment_id is not None:
                final_status, patient_rows, patient_scope = self.repository.apply_appointment_action(
                    booking_id=booking.id,
                    appointment_id=appointment_id,
                    user_id=user_id,
                    action=normalized_action,
                    start_time=(getattr(payload, "start_time", None) if payload is not None else None),
                    start_location=(getattr(payload, "start_location", None) if payload is not None else None),
                    complete_time=(getattr(payload, "complete_time", None) if payload is not None else None),
                    complete_location=(getattr(payload, "complete_location", None) if payload is not None else None),
                )
                source_type = "APPOINTMENT"
                detail = f"Appointment action '{normalized_action}' applied successfully"
            else:
                if normalized_action == "complete" and payload is not None and (getattr(payload, "patient_updates", None) or []):
                    final_status, patient_rows = self.repository.apply_booking_completion_patientwise(
                        booking_id=booking.id,
                        updates=(getattr(payload, "patient_updates", None) or []),
                        actor_user_id=user_id,
                        complete_time=(getattr(payload, "complete_time", None) if payload is not None else None),
                        complete_location=(getattr(payload, "complete_location", None) if payload is not None else None),
                    )
                else:
                    final_status, patient_rows = self.repository.apply_booking_action(
                        booking_id=booking.id,
                        action=normalized_action,
                        start_time=(getattr(payload, "start_time", None) if payload is not None else None),
                        start_location=(getattr(payload, "start_location", None) if payload is not None else None),
                        complete_time=(getattr(payload, "complete_time", None) if payload is not None else None),
                        complete_location=(getattr(payload, "complete_location", None) if payload is not None else None),
                    )
                patient_scope = "BOOKING_ALL_FALLBACK"
                source_type = "BOOKING"
                detail = f"Booking action '{normalized_action}' applied successfully"
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            if completion_lock_acquired:
                self.repository.release_booking_completion_lock(booking.id)

        response = BookingStatusUpdateResponse(
            booking_id=booking.id,
            booking_status=final_status,
            action=action,
            patients=patient_rows,
            detail=detail,
            source_type=source_type,
            appointment_id=appointment_id,
            patient_scope=patient_scope,
        )
        if normalized_action == "complete" and appointment_id is None:
            try:
                service_note_payload = self.build_service_note_payload_by_id(
                    booking_id=(None if appointment_id is not None else int(booking.id)),
                    appointment_id=(int(appointment_id) if appointment_id is not None else None),
                )
                submit_pdf_generation(service_note_payload, booking_id=int(booking.id))
                self._logger.info(
                    "[service note pdf] queued after completion booking_id=%s appointment_id=%s",
                    booking.id,
                    appointment_id,
                )
            except Exception:
                self._logger.exception(
                    "[service note pdf] post-completion queue failed booking_id=%s appointment_id=%s",
                    booking.id,
                    appointment_id,
                )
        elif normalized_action == "complete":
            self._logger.info(
                "[service note pdf] skipped for appointment completion booking_id=%s appointment_id=%s",
                booking.id,
                appointment_id,
            )
        self._logger.info(
            "booking_status_timing booking_id=%s appointment_id=%s user_id=%s action=%s lock_wait_ms=%.2f tests_ms=%.2f uploads_ms=%.2f total_ms=%.2f",
            booking_id,
            appointment_id,
            user_id,
            normalized_action,
            lock_wait_ms,
            tests_phase_ms,
            (perf_counter() - uploads_phase_started_at) * 1000 if uploads_phase_started_at else 0.0,
            (perf_counter() - request_started_at) * 1000,
        )
        return response
    def cancel_assigned_booking_direct(
        self,
        booking_id: int,
        user_id: int,
        reason_text: str,
        remark: str | None = None,
        reschedule_requested: bool = False,
        proposed_visit_date: str | None = None,
        proposed_time_slot: str | None = None,
        appointment_id: int | None = None,
        complete_time: str | None = None,
        complete_location: str | None = None,
    ) -> dict:
        appointment_context = appointment_id is not None and int(appointment_id or 0) > 0
        if appointment_context:
            booking = self.repository.get_booking_by_id(booking_id=booking_id)
            if not booking:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Booking not found",
                )
        else:
            booking = self.repository.get_assigned_booking_by_id(
                booking_id=booking_id,
                user_id=user_id,
                exclude_cancelled=False,
            )
            if not booking:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Booking not found or not assigned to current user",
                )

        reason = str(reason_text or "").strip()
        if not reason:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancel reason is required")

        if reschedule_requested and (not str(proposed_visit_date or "").strip() or not str(proposed_time_slot or "").strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reschedule date and time slot are required when reschedule is requested",
            )

        try:
            if appointment_context:
                status_code, _patient_rows, _scope = self.repository.apply_appointment_action(
                    booking_id=booking.id,
                    appointment_id=int(appointment_id),
                    user_id=user_id,
                    action="cancel",
                    complete_time=complete_time,
                    complete_location=complete_location,
                )
                return {
                    "ok": True,
                    "booking_id": int(booking.id),
                    "booking_status": int(status_code),
                    "lead_created": False,
                    "lead_id": None,
                    "detail": "Appointment cancelled successfully",
                }

            status_code, lead_created, lead_id = self.repository.cancel_booking_with_lead(
                booking_id=booking.id,
                actor_user_id=user_id,
                reason_text=reason,
                remark=remark,
                complete_time=complete_time,
                complete_location=complete_location,
                reschedule_requested=bool(reschedule_requested),
                proposed_visit_date=(str(proposed_visit_date).strip() if proposed_visit_date else None),
                proposed_time_slot=(str(proposed_time_slot).strip() if proposed_time_slot else None),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        return {
            "ok": True,
            "booking_id": int(booking.id),
            "booking_status": int(status_code),
            "lead_created": bool(lead_created),
            "lead_id": lead_id,
            "detail": "Booking cancelled successfully",
        }

    def add_patient_to_existing_booking(
        self,
        booking_id: int,
        user_id: int,
        payload: AddPatientToBookingRequest,
        patient_documents: list | None = None,
    ) -> AddPatientToBookingResponse:
        booking = self.repository.get_assigned_booking_by_id(
            booking_id=booking_id,
            user_id=user_id,
            exclude_cancelled=False,
        )
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found or not assigned to current user",
            )

        current_status = int(booking.booking_status or 0)
        if current_status not in {1, 2}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Patient can be added only when booking is in assigned or started status",
            )

        if payload.existing_patient_id is not None:
            if patient_documents:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="patient_documents are not allowed when linking an existing patient",
                )
            try:
                result = self.repository.link_existing_patient_to_booking_same_address(
                    booking_id=booking.id,
                    caller_id=int(booking.caller_id),
                    patient_id=int(payload.existing_patient_id),
                    address_id=int(booking.selected_address_id),
                    booking_status=current_status,
                    actor_user_id=user_id or 1,
                    auto_commit=True,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            self._logger.info(
                "PATIENT_LINKED_TO_BOOKING booking_id=%s patient_id=%s user=%s",
                result.get("booking_id"),
                result.get("patient_id"),
                user_id,
            )
            return AddPatientToBookingResponse(ok=True, **result)

        primary_mobile = self._normalize_mobile(payload.primary_mobile or "")
        alternate_mobile = (
            self._normalize_mobile(payload.alternate_mobile)
            if payload.alternate_mobile
            else None
        )

        base_payload = {
            "title": payload.title,
            "full_name": (payload.full_name or "").strip(),
            "gender": (payload.gender or "").strip(),
            "date_of_birth": payload.date_of_birth,
            "age_years": payload.age_years,
            "primary_mobile": primary_mobile,
            "primary_mobile_norm": primary_mobile,
            "primary_mobile_raw": (payload.primary_mobile or "").strip(),
            "alternate_mobile": alternate_mobile,
            "alternate_mobile_norm": alternate_mobile,
            "alternate_mobile_raw": payload.alternate_mobile.strip()
            if payload.alternate_mobile
            else None,
            "email": payload.email,
            "labmate_pid": payload.labmate_pid,
            "panel_company": payload.panel_company,
            "tag": payload.tag,
        }

        files = [f for f in (patient_documents or []) if f is not None]
        if not files:
            try:
                result = self.repository.add_patient_to_booking_same_address(
                    booking_id=booking.id,
                    caller_id=int(booking.caller_id),
                    address_id=int(booking.selected_address_id),
                    booking_status=current_status,
                    payload=base_payload,
                    actor_user_id=user_id or 1,
                    auto_commit=True,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            self._logger.info(
                "PATIENT_CREATED_AND_LINKED_TO_BOOKING booking_id=%s patient_id=%s user=%s",
                result.get("booking_id"),
                result.get("patient_id"),
                user_id,
            )
            return AddPatientToBookingResponse(ok=True, **result)

        saved_abs_paths: list[Path] = []
        try:
            result = self.repository.add_patient_to_booking_same_address(
                booking_id=booking.id,
                caller_id=int(booking.caller_id),
                address_id=int(booking.selected_address_id),
                booking_status=current_status,
                payload=base_payload,
                actor_user_id=user_id or 1,
                auto_commit=False,
            )

            patient_id = int(result["patient_id"])
            existing_documents = self.repository.get_patient_document_paths(patient_id=patient_id)
            document_paths = self._save_patient_documents(
                patient_id=patient_id,
                files=files,
                existing_documents=existing_documents,
                saved_abs_paths=saved_abs_paths,
            )
            self.repository.update_patient_documents(
                patient_id=patient_id,
                documents=document_paths,
            )
            self.repository.db.commit()
            self._logger.info(
                "PATIENT_CREATED_AND_LINKED_TO_BOOKING booking_id=%s patient_id=%s user=%s",
                result.get("booking_id"),
                result.get("patient_id"),
                user_id,
            )
            return AddPatientToBookingResponse(
                ok=True,
                uploaded_documents=document_paths,
                uploaded_documents_count=len(document_paths),
                **result,
            )
        except HTTPException:
            self.repository.db.rollback()
            self._cleanup_saved_files(saved_abs_paths)
            raise
        except ValueError as exc:
            self.repository.db.rollback()
            self._cleanup_saved_files(saved_abs_paths)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except Exception:
            self.repository.db.rollback()
            self._cleanup_saved_files(saved_abs_paths)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload patient documents",
            )


    def _shared_upload_root(self) -> Path:
        return Path(self._settings.patient_documents_upload_base).resolve().parent

    @staticmethod
    def _cleanup_saved_files(paths: list[Path]) -> None:
        for path in reversed(paths):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                continue
        for path in reversed(paths):
            try:
                parent = path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                continue

    def _save_patient_documents(
        self,
        patient_id: int,
        files: list,
        existing_documents: list[str],
        saved_abs_paths: list[Path],
        file_types: list[str] | None = None,
    ) -> list[str]:
        if len(existing_documents) + len(files) > self._max_documents_per_patient:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Maximum {self._max_documents_per_patient} documents allowed per patient"
                ),
            )

        rel_dir = Path(f"PT{patient_id}")
        base_dir = Path(self._settings.patient_documents_upload_base) / rel_dir
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_rel_paths: list[str] = list(existing_documents)
        seq = len(existing_documents) + 1
        normalized_types = [str(t or "").strip().lower() for t in (file_types or [])]
        for idx, file in enumerate(files):
            filename = str(getattr(file, "filename", "") or "").strip()
            ext = Path(filename).suffix.lower()
            if ext not in self._allowed_document_ext:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Invalid file extension. Allowed: .pdf, .jpg, .jpeg, .png"
                    ),
                )

            dtype = normalized_types[idx] if idx < len(normalized_types) else ""
            if dtype == "cghs_card":
                out_name = f"PT{patient_id}_CGHS_{seq}{ext}"
            elif dtype == "patient_photo":
                out_name = f"PT{patient_id}_PHOTO_{seq}{ext}"
            else:
                out_name = f"PT{patient_id}_DOC_{seq}{ext}"
            seq += 1
            out_path = base_dir / out_name

            file_obj = getattr(file, "file", None)
            if file_obj is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid uploaded file payload",
                )
            content = file_obj.read()
            if content is None:
                content = b""
            out_path.write_bytes(content)
            saved_abs_paths.append(out_path)
            rel_saved = f"{rel_dir.as_posix()}/{out_name}"
            saved_rel_paths.append(rel_saved)
        return saved_rel_paths

    def _save_booking_prescriptions(
        self,
        booking_code: str,
        patient_id: int,
        files: list,
        existing_prescriptions: list[str],
        saved_abs_paths: list[Path],
    ) -> list[str]:
        clean_booking_code = str(booking_code or "").strip()
        if not clean_booking_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Booking code is required for prescription upload",
            )

        prescription_base = Path(self._settings.patient_documents_upload_base).parent / "prescriptions"
        rel_dir = Path(clean_booking_code)
        base_dir = prescription_base / rel_dir
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_rel_paths: list[str] = list(existing_prescriptions)
        seq = len(existing_prescriptions) + 1
        for file in files:
            filename = str(getattr(file, "filename", "") or "").strip()
            ext = Path(filename).suffix.lower()
            if ext not in self._allowed_document_ext:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file extension. Allowed: .pdf, .jpg, .jpeg, .png",
                )

            out_name = f"{clean_booking_code}_PT{int(patient_id)}_{seq}{ext}"
            seq += 1
            out_path = base_dir / out_name

            file_obj = getattr(file, "file", None)
            if file_obj is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid uploaded file payload",
                )
            content = file_obj.read()
            if content is None:
                content = b""
            out_path.write_bytes(content)
            saved_abs_paths.append(out_path)
            rel_saved = f"{rel_dir.as_posix()}/{out_name}"
            saved_rel_paths.append(rel_saved)
        return saved_rel_paths

    def _save_booking_patient_photos(
        self,
        booking_code: str,
        patient_id: int,
        files: list,
        existing_photos: list[str],
        saved_abs_paths: list[Path],
    ) -> list[str]:
        clean_booking_code = str(booking_code or "").strip()
        if not clean_booking_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Booking code is required for patient photo upload",
            )

        photo_base = Path(self._settings.patient_documents_upload_base).parent / "booking_patient_documents"
        rel_dir = Path(clean_booking_code) / f"PT{int(patient_id)}"
        base_dir = photo_base / rel_dir
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_rel_paths: list[str] = list(existing_photos)
        seq = len(existing_photos) + 1
        for file in files:
            filename = str(getattr(file, "filename", "") or "").strip()
            ext = Path(filename).suffix.lower()
            if ext not in self._allowed_document_ext:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid patient photo extension. Allowed: .pdf, .jpg, .jpeg, .png",
                )

            out_name = f"{clean_booking_code}_PT{int(patient_id)}_PHOTO_{seq}{ext}"
            seq += 1
            out_path = base_dir / out_name

            file_obj = getattr(file, "file", None)
            if file_obj is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid uploaded file payload",
                )
            content = file_obj.read()
            if content is None:
                content = b""
            out_path.write_bytes(content)
            saved_abs_paths.append(out_path)
            rel_saved = f"{rel_dir.as_posix()}/{out_name}"
            saved_rel_paths.append(rel_saved)
        return saved_rel_paths

    def _is_manual_hcb_slip(self, value: object) -> bool:
        v = str(value or "").strip().lower()
        return v in {"manual hcb slip", "manual_hcb_slip", "manual hc slip", "manual_hc_slip", "manual_slip", "manual-slip", "hcb_slip", "hcb-slip"}

    def _save_hc_slip_files(
        self,
        booking_code: str,
        patient_id: int,
        files: list,
    ) -> list[str]:
        clean_booking_code = str(booking_code or "").strip()
        if not clean_booking_code:
            return []
        base_dir = self._shared_upload_root() / "hc_slip" / clean_booking_code / f"PT{int(patient_id)}"
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_rel_paths: list[str] = []
        seq = 1
        for file in (files or []):
            filename = str(getattr(file, "filename", "") or "").strip()
            ext = Path(filename).suffix.lower()
            if ext not in self._allowed_document_ext:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid HC slip extension. Allowed: .pdf, .jpg, .jpeg, .png",
                )
            if seq == 1:
                out_name = f"{clean_booking_code}_PT{int(patient_id)}_HC_SLIP{ext}"
            else:
                out_name = f"{clean_booking_code}_PT{int(patient_id)}_HC_SLIP_{seq}{ext}"
            seq += 1
            out_path = base_dir / out_name
            file_obj = getattr(file, "file", None)
            if file_obj is None:
                continue
            content = file_obj.read() or b""
            out_path.write_bytes(content)
            rel_saved = f"hc_slip/{clean_booking_code}/PT{int(patient_id)}/{out_name}"
            saved_rel_paths.append(rel_saved)
        return saved_rel_paths

    def _save_payment_screenshots(
        self,
        booking_code: str,
        patient_id: int,
        files: list,
        category: str | None = None,
        name_mode: str = "pay",
    ) -> list[str]:
        clean_booking_code = str(booking_code or "").strip()
        if not clean_booking_code:
            return []
        folder = str(category or "").strip().lower()
        if folder:
            base_dir = self._shared_upload_root() / "payment_shot" / folder / clean_booking_code / f"PT{int(patient_id)}"
        else:
            base_dir = self._shared_upload_root() / "payment_shot" / clean_booking_code / f"PT{int(patient_id)}"
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_rel_paths: list[str] = []
        seq = 1
        for file in (files or []):
            filename = str(getattr(file, "filename", "") or "").strip()
            ext = Path(filename).suffix.lower()
            if ext not in self._allowed_document_ext:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid payment screenshot extension. Allowed: .pdf, .jpg, .jpeg, .png",
                )
            mode = str(name_mode or "pay").strip().lower()
            if mode == "hc_slip":
                if seq == 1:
                    out_name = f"{clean_booking_code}_PT{int(patient_id)}_HC_SLIP{ext}"
                else:
                    out_name = f"{clean_booking_code}_PT{int(patient_id)}_HC_SLIP_{seq}{ext}"
            else:
                out_name = f"{clean_booking_code}_PT{int(patient_id)}_PAY_{seq}{ext}"
            seq += 1
            out_path = base_dir / out_name
            file_obj = getattr(file, "file", None)
            if file_obj is None:
                continue
            content = file_obj.read() or b""
            out_path.write_bytes(content)
            if folder:
                rel_saved = f"{folder}/{clean_booking_code}/PT{int(patient_id)}/{out_name}"
            else:
                rel_saved = f"{clean_booking_code}/PT{int(patient_id)}/{out_name}"
            saved_rel_paths.append(rel_saved)
        return saved_rel_paths

    def edit_patient_in_existing_booking(
        self,
        booking_id: int,
        patient_id: int,
        user_id: int,
        payload: EditPatientInBookingRequest,
        patient_documents: list | None = None,
    ) -> EditPatientInBookingResponse:
        booking = self.repository.get_assigned_booking_by_id(
            booking_id=booking_id,
            user_id=user_id,
            exclude_cancelled=False,
        )
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found or not assigned to current user",
            )

        requested_patient_id = patient_id
        context = self.repository.get_booking_patient_context(
            booking_id=booking_id,
            patient_id=requested_patient_id,
        )
        if not context:
            resolved_patient_id = self.repository.get_patient_id_by_booking_patient_id(
                booking_id=booking_id,
                booking_patient_id=requested_patient_id,
            )
            if resolved_patient_id:
                context = self.repository.get_booking_patient_context(
                    booking_id=booking_id,
                    patient_id=resolved_patient_id,
                )
                requested_patient_id = resolved_patient_id
        if not context:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient is not mapped to this booking",
            )

        updatable_fields: dict = {}
        if payload.title is not None:
            updatable_fields["title"] = payload.title
        if payload.full_name is not None:
            updatable_fields["full_name"] = payload.full_name.strip()
        if payload.gender is not None:
            updatable_fields["gender"] = payload.gender.strip()
        if payload.date_of_birth is not None:
            updatable_fields["date_of_birth"] = payload.date_of_birth
        if payload.age_years is not None:
            updatable_fields["age_years"] = payload.age_years
        if payload.labmate_pid is not None:
            updatable_fields["labmate_pid"] = payload.labmate_pid
        if payload.panel_company is not None:
            updatable_fields["panel_company"] = payload.panel_company
        if payload.tag is not None:
            updatable_fields["tag"] = payload.tag

        primary_norm = None
        primary_raw = None
        old_primary_norm = self._normalize_mobile(str(context.get("contact_mobile"))) if context.get("contact_mobile") else None
        if payload.primary_mobile is not None:
            primary_raw_candidate = payload.primary_mobile.strip()
            if primary_raw_candidate:
                primary_norm = self._normalize_mobile(primary_raw_candidate)
                primary_raw = primary_raw_candidate
                updatable_fields["primary_mobile"] = primary_norm

        alternate_norm = None
        alternate_raw = None
        old_alternate_norm = self._normalize_mobile(str(context.get("alternate_mobile"))) if context.get("alternate_mobile") else None
        if payload.alternate_mobile is not None:
            alternate_raw_candidate = payload.alternate_mobile.strip()
            if alternate_raw_candidate:
                alternate_norm = self._normalize_mobile(alternate_raw_candidate)
                alternate_raw = alternate_raw_candidate
                updatable_fields["alternate_mobile"] = alternate_norm

        files = [f for f in (patient_documents or []) if f is not None]
        if not updatable_fields and not files:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No editable fields or documents provided",
            )

        result = {
            "booking_id": booking_id,
            "patient_id": requested_patient_id,
            "linked_mobiles": [],
            "message": "Patient updated successfully",
        }
        saved_abs_paths: list[Path] = []
        try:
            if updatable_fields:
                result = self.repository.edit_patient_in_booking(
                    booking_id=booking_id,
                    patient_id=requested_patient_id,
                    caller_id=int(context["caller_id"]),
                    actor_user_id=user_id or 1,
                    update_fields=updatable_fields,
                    old_primary_mobile_norm=old_primary_norm,
                    new_primary_mobile_norm=primary_norm,
                    old_alternate_mobile_norm=old_alternate_norm,
                    new_alternate_mobile_norm=alternate_norm,
                    primary_mobile_raw=primary_raw,
                    alternate_mobile_raw=alternate_raw,
                )

            if files:
                existing_documents = self.repository.get_patient_document_paths(patient_id=requested_patient_id)
                document_paths = self._save_patient_documents(
                    patient_id=requested_patient_id,
                    files=files,
                    existing_documents=existing_documents,
                    saved_abs_paths=saved_abs_paths,
                )
                self.repository.update_patient_documents(
                    patient_id=requested_patient_id,
                    documents=document_paths,
                )
                if updatable_fields:
                    self.repository.db.commit()
                    result["message"] = "Patient and documents updated successfully"
                else:
                    result["message"] = "Patient documents updated successfully"
        except HTTPException:
            self._cleanup_saved_files(saved_abs_paths)
            raise
        except ValueError as exc:
            self._cleanup_saved_files(saved_abs_paths)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except Exception:
            self._cleanup_saved_files(saved_abs_paths)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update patient documents",
            )

        return EditPatientInBookingResponse(
            ok=True,
            patient_code=str(context["patient_code"]),
            **result,
        )


    def _max_allowed_discount_from_panelrates(self, catalog_db: Session | None, comp_cat_id: str | None, booked_code: str | None, mrp: float) -> float:
        if not catalog_db:
            return 0.0
        comp = str(comp_cat_id or "").strip()
        code = str(booked_code or "").strip().upper()
        if not comp or not code or mrp <= 0:
            return 0.0
        m = re.match(r"^(G\d{2})?(S\d{2})?(T\d+)$", code, flags=re.IGNORECASE)
        if not m:
            return 0.0
        g, s, t = (m.group(1) or "").upper(), (m.group(2) or "").upper(), (m.group(3) or "").upper()
        if not (g and s and t):
            return 0.0
        row = catalog_db.execute(
            text("SELECT MaximumpercentageAllowed FROM panelrates WHERE CompCatID=:comp AND BookedFlag=1 AND GCode=:g AND SCode=:s AND TestCode=:t ORDER BY ABS(COALESCE(MRP,0)-:mrp) LIMIT 1"),
            {"comp": comp, "g": g, "s": s, "t": t, "mrp": float(mrp)},
        ).mappings().first()
        if not row:
            return 0.0
        pct = float(row.get("MaximumpercentageAllowed") or 0)
        return round((float(mrp) * pct) / 100.0, 2) if pct > 0 else 0.0

    def save_assigned_booking_tests(
        self,
        booking_id: int,
        user_id: int,
        payload: MobileBookingTestsSaveRequest,
        catalog_db: Session | None = None,
    ) -> MobileBookingTestsSaveResponse:
        booking = self.repository.get_assigned_booking_by_id(
            booking_id=booking_id,
            user_id=user_id,
            exclude_cancelled=False,
        )
        if not booking:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found or not assigned to current user")

        desired_rows: list[dict] = []
        patient_panel_map: dict[int, dict] = {}
        subtotal = 0.0
        base_discount = 0.0
        max_total_discount = 0.0
        credit_amount = 0.0
        paying_amount = 0.0

        for p in (payload.tests_payload or []):
            patient_id = int(p.patient_id)
            panel_comp_ids: list[str] = []
            panel_modes: list[str] = []
            panel_names: list[str] = []
            for panel in (p.panels or []):
                comp_cat_id = (panel.comp_cat_id or "").strip()
                selected_mode = (panel.selected_charge_mode or "").strip().upper()
                panel_name = (panel.panel_company or "").strip()
                if comp_cat_id and comp_cat_id not in panel_comp_ids:
                    panel_comp_ids.append(comp_cat_id)
                    panel_modes.append(selected_mode)
                    panel_names.append(panel_name)
                for t in (panel.selected_tests or []):
                    booked_code = str(t.booked_code or "").strip().upper()
                    if not booked_code:
                        continue
                    mrp = float(t.mrp or 0)
                    max_discount = float(t.max_discount or 0)
                    max_allowed = float(t.max_allowed_discount or 0)
                    if max_allowed <= 0:
                        max_allowed = self._max_allowed_discount_from_panelrates(catalog_db, comp_cat_id, booked_code, mrp)
                    if max_allowed < max_discount:
                        max_allowed = max_discount
                    subtotal += mrp
                    base_discount += max_discount
                    max_total_discount += max_allowed
                    desired_rows.append({
                        "patient_id": patient_id,
                        "comp_cat_id": panel.comp_cat_id,
                        "booked_code": booked_code,
                        "test_name": t.description,
                        "charge": float(t.charge or 0),
                        "mrp": mrp,
                        "max_discount": max_discount,
                    })
            patient_panel_map[patient_id] = {
                "selected_comp_cat_ids": ",".join(panel_comp_ids) or None,
                "selected_charge_modes": ",".join(panel_modes) or None,
                "selected_panel_companies": ",".join(panel_names) or None,
            }

        mode = str(payload.additional_discount_mode or "").strip().lower()
        raw_value = float(payload.additional_discount_value or 0)
        if raw_value < 0:
            raw_value = 0.0
        requested_additional = (subtotal * raw_value / 100.0) if mode == "percent" else (raw_value if mode == "amount" else 0.0)
        max_additional_allowed = max(0.0, max_total_discount - base_discount)
        if requested_additional > max_additional_allowed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"You can apply additional discount up to {max_additional_allowed:.2f} only. You have entered {requested_additional:.2f}.")

        effective_additional = min(requested_additional, max_additional_allowed)
        final_discount = base_discount + effective_additional
        final_amount = max(0.0, subtotal - final_discount)

        active_count, dropped_count = self.repository.save_booking_tests_and_amounts(
            booking_id=booking_id,
            actor_user_id=user_id,
            desired_rows=desired_rows,
            patient_panel_map=patient_panel_map,
            subtotal=subtotal,
            final_discount=final_discount,
            additional_discount=effective_additional,
            final_amount=final_amount,
            credit_amount=credit_amount,
            paying_amount=paying_amount,
        )

        return MobileBookingTestsSaveResponse(
            ok=True,
            booking_id=booking_id,
            saved_amounts=BookingAmounts(
                subtotal=round(subtotal, 2),
                base_discount=round(base_discount, 2),
                additional=round(effective_additional, 2),
                final_discount=round(final_discount, 2),
                final_amount=round(final_amount, 2),
            ),
            active_tests_count=active_count,
            dropped_tests_count=dropped_count,
        )


















    def get_my_batch_handover_history(
        self,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> BatchListResponse:
        rows = self.repository.list_hhome_collection_batch_for_user(
            created_by=user_id,
            limit=limit,
            offset=offset,
        )

        items: list[BatchListItem] = []
        for row in rows:
            try:
                batch = json.loads(str(row.get("batch_json") or "{}"))
            except Exception:
                batch = {}
            try:
                booking_ids = json.loads(str(row.get("booking_ids") or "[]"))
            except Exception:
                booking_ids = []
            try:
                patients = json.loads(str(row.get("patients_json") or "[]"))
            except Exception:
                patients = []
            try:
                tubes = json.loads(str(row.get("tubes_json") or "[]"))
            except Exception:
                tubes = []
            created_at = row.get("created_at")
            items.append(
                BatchListItem(
                    id=int(row.get("id") or 0),
                    batch_code=(str(row.get("batch_code")).strip() if row.get("batch_code") is not None else None),
                    batch=batch if isinstance(batch, dict) else {},
                    booking_ids=[int(x) for x in (booking_ids or []) if str(x).strip().isdigit()],
                    patients=patients if isinstance(patients, list) else [],
                    tubes=tubes if isinstance(tubes, list) else [],
                    created_at=str(created_at) if created_at is not None else None,
                )
            )

        return BatchListResponse(items=items)

    @staticmethod
    def _safe_json_list(raw_value: object) -> list:
        if isinstance(raw_value, list):
            return raw_value
        if not raw_value:
            return []
        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else []
        except Exception:
            parsed = []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _normalize_tube_names(*values: object) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            if isinstance(value, list):
                parts = value
            else:
                raw = str(value or "").strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    parts = parsed
                else:
                    parts = raw.replace("|", ",").split(",")
            for item in parts:
                name = str(item or "").strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(name)
        return out

    @staticmethod
    def _batch_patient_key(row: dict) -> tuple[str, int, int | None, int, int]:
        source_type = str(row.get("source_type") or row.get("sourceType") or "").strip().upper()
        if source_type not in {"BOOKING", "APPOINTMENT"}:
            source_type = "APPOINTMENT" if row.get("appointment_id") or row.get("appointmentId") else "BOOKING"
        try:
            booking_id = int(row.get("booking_id") or row.get("bookingId") or 0)
        except Exception:
            booking_id = 0
        appt_raw = row.get("appointment_id")
        if appt_raw is None:
            appt_raw = row.get("appointmentId")
        try:
            appointment_id = int(appt_raw) if appt_raw is not None and str(appt_raw).strip() else None
        except Exception:
            appointment_id = None
        try:
            booking_patient_id = int(row.get("booking_patient_id") or row.get("bookingPatientId") or 0)
        except Exception:
            booking_patient_id = 0
        try:
            patient_id = int(row.get("patient_id") or row.get("patientId") or 0)
        except Exception:
            patient_id = 0
        return source_type, booking_id, appointment_id, booking_patient_id, patient_id

    def get_my_batch_ready_bookings(self, *, user_id: int) -> BatchReadyResponse:
        already_batched: set[tuple[str, int, int | None, int, int]] = set()
        for batch_row in self.repository.list_hhome_collection_batch_payloads_for_user(created_by=user_id):
            for raw_json in (batch_row.get("patients_json"), batch_row.get("tubes_json")):
                for item in self._safe_json_list(raw_json):
                    if not isinstance(item, dict):
                        continue
                    key = self._batch_patient_key(item)
                    if key[1] > 0 and (key[3] > 0 or key[4] > 0):
                        already_batched.add(key)

        rows_by_booking: dict[tuple[int, int | None], dict] = {}

        def add_patient(
            *,
            booking_id: int,
            appointment_id: int | None,
            booking_patient_id: int,
            patient_id: int,
            patient_name: str | None,
            tube_names: list[str],
        ) -> None:
            source_type = "APPOINTMENT" if appointment_id else "BOOKING"
            key = (source_type, int(booking_id), appointment_id, int(booking_patient_id or 0), int(patient_id or 0))
            if key in already_batched:
                return
            if not tube_names:
                return
            group_key = (int(booking_id), appointment_id)
            group = rows_by_booking.setdefault(
                group_key,
                {
                    "booking_id": int(booking_id),
                    "appointment_id": appointment_id,
                    "booking_code": None,
                    "patients": [],
                },
            )
            group["patients"].append(
                BatchPatientItem(
                    patient_id=int(patient_id),
                    booking_patient_id=int(booking_patient_id),
                    patient_name=(str(patient_name).strip() if patient_name else None),
                    tubes=[BatchTubeItem(tube_name=x) for x in tube_names],
                )
            )

        for row in self.repository.list_completed_booking_patients_for_batch(user_id=user_id):
            add_patient(
                booking_id=int(row.get("booking_id") or 0),
                appointment_id=None,
                booking_patient_id=int(row.get("booking_patient_id") or 0),
                patient_id=int(row.get("patient_id") or 0),
                patient_name=(str(row.get("patient_name")).strip() if row.get("patient_name") is not None else None),
                tube_names=self._normalize_tube_names(row.get("cmplt_tube"), row.get("additional_sample")),
            )

        appointment_rows = self.repository.list_completed_appointments_for_batch(user_id=user_id)
        appointment_booking_ids = [int(x.get("booking_id") or 0) for x in appointment_rows]
        patient_lookup: dict[tuple[int, int], dict] = {}
        for p in self.repository.list_booking_patients_for_bookings(booking_ids=appointment_booking_ids):
            try:
                patient_lookup[(int(p.get("booking_id") or 0), int(p.get("patient_id") or 0))] = p
            except Exception:
                continue

        for row in appointment_rows:
            booking_id = int(row.get("booking_id") or 0)
            appointment_id = int(row.get("appointment_id") or 0)
            payload = self._safe_json_dict(row.get("cmplt_tube"))
            patients = payload.get("patients") if isinstance(payload, dict) else []
            if not isinstance(patients, list):
                continue
            for item in patients:
                if not isinstance(item, dict):
                    continue
                try:
                    patient_id = int(item.get("patient_id") or 0)
                except Exception:
                    patient_id = 0
                if patient_id <= 0:
                    continue
                lookup = patient_lookup.get((booking_id, patient_id), {})
                booking_patient_id = int(item.get("booking_patient_id") or lookup.get("booking_patient_id") or 0)
                if booking_patient_id <= 0:
                    continue
                add_patient(
                    booking_id=booking_id,
                    appointment_id=appointment_id,
                    booking_patient_id=booking_patient_id,
                    patient_id=patient_id,
                    patient_name=(str(lookup.get("patient_name")).strip() if lookup.get("patient_name") is not None else None),
                    tube_names=self._normalize_tube_names(item.get("cmplt_tube"), item.get("additional_sample")),
                )

        bookings = [
            BatchBookingItem(
                booking_id=int(group["booking_id"]),
                appointment_id=group["appointment_id"],
                booking_code=group.get("booking_code"),
                patients=group["patients"],
            )
            for group in rows_by_booking.values()
            if group.get("patients")
        ]
        return BatchReadyResponse(bookings=bookings)

    def save_batch_handover(
        self,
        *,
        user_id: int,
        payload: BatchSaveRequest,
    ) -> BatchSaveResponse:
        batch_meta = payload.batch.model_dump() if payload.batch else {}
        booking_ids: list[int] = []
        patients_rows: list[dict] = []
        tubes_rows: list[dict] = []

        for b in (payload.bookings or []):
            bid = int(b.booking_id)
            appointment_id = int(b.appointment_id) if b.appointment_id is not None else None
            source_type = "APPOINTMENT" if appointment_id else "BOOKING"
            booking_ids.append(bid)
            for p in (b.patients or []):
                pid = int(p.patient_id)
                bpid = int(p.booking_patient_id)
                pname = (p.patient_name or "").strip() or None
                patients_rows.append({
                    "booking_id": bid,
                    "appointment_id": appointment_id,
                    "source_type": source_type,
                    "booking_code": b.booking_code,
                    "patient_id": pid,
                    "booking_patient_id": bpid,
                    "patient_name": pname,
                })
                for t in (p.tubes or []):
                    tname = (t.tube_name or "").strip()
                    if not tname:
                        continue
                    tubes_rows.append({
                        "booking_id": bid,
                        "appointment_id": appointment_id,
                        "source_type": source_type,
                        "booking_code": b.booking_code,
                        "patient_id": pid,
                        "booking_patient_id": bpid,
                        "patient_name": pname,
                        "tube_name": tname,
                    })

        batch_id = self.repository.insert_hhome_collection_batch(
            batch_json=batch_meta,
            booking_ids=sorted(set(booking_ids)),
            patients_json=patients_rows,
            tubes_json=tubes_rows,
            created_by=int(user_id),
        )
        return BatchSaveResponse(ok=True, batch_id=int(batch_id), detail="Batch saved successfully")

    def edit_booking_address(
        self,
        booking_id: int,
        user_id: int,
        payload: EditBookingAddressRequest,
    ) -> EditBookingAddressResponse:
        booking = self.repository.get_assigned_booking_by_id(
            booking_id=booking_id,
            user_id=user_id,
            exclude_cancelled=False,
        )
        if not booking:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Booking not found or not assigned to current user",
            )

        current_status = int(booking.booking_status or 0)
        if current_status not in {0, 1, 2, 5}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Address can be updated only for pending/assigned/started/mixed bookings",
            )

        target_address_id = int(payload.address_id or booking.selected_address_id or 0)
        if target_address_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Address id is required",
            )
        if int(booking.selected_address_id or 0) != target_address_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only selected booking address can be edited",
            )

        fields = {
            "address_type": payload.address_type,
            "house_flat_no": payload.house_flat_no,
            "floor": payload.floor,
            "block_tower_no": payload.block_tower_no,
            "street_line": payload.street_line,
            "landmark": payload.landmark,
            "colony_name": payload.colony_name,
            "pincode": payload.pincode,
            "route_no": payload.route_no,
            "city": payload.city,
            "google_location": payload.google_location,
            "access_notes": payload.access_notes,
        }
        updated = self.repository.update_booking_address(
            booking_id=booking_id,
            address_id=target_address_id,
            fields=fields,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Address not found",
            )

        return EditBookingAddressResponse(
            ok=True,
            booking_id=int(booking_id),
            address_id=int(target_address_id),
            message="Address updated successfully",
            address=AddressDetails.model_validate(updated),
        )


