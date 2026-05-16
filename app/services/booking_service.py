from pathlib import Path
import json
import logging

from fastapi import HTTPException, status
from sqlalchemy import bindparam, text
import re
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.booking_repository import BookingRepository
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
    BatchSaveRequest,
    BatchSaveResponse,
    BatchListItem,
    BatchListResponse,
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
        allowed = {int(x) for x in (patient_ids or []) if str(x).isdigit()} if patient_ids else None
        result: dict[int, list[dict]] = {}

        all_keys = set(tests_map.keys()) | set(pending_map.keys())
        for pid_key in all_keys:
            try:
                pid = int(pid_key)
            except Exception:
                continue
            if allowed is not None and pid not in allowed:
                continue

            tests_out: list[dict] = []
            seen_codes: set[str] = set()
            parent_codes: set[str] = set()

            pending_selected = (pending_map.get(pid_key) or {}).get("selected_tests") or []
            for item in pending_selected:
                code = self._as_str(item.get("booked_code"))
                if not code:
                    continue
                parent_code = self._as_str(item.get("parent_booked_code"))
                if parent_code:
                    parent_codes.add(parent_code)
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                tests_out.append({
                    "booked_code": code,
                    "test_name": self._as_str(item.get("description")) or self._as_str(item.get("test_name")) or code,
                    "test_status": 0,
                    # Pending-carried child tests are informational only in appointment scope.
                    "mrp": 0.0,
                    "charge": 0.0,
                    "max_discount": 0.0,
                })

            tests_selected = (tests_map.get(pid_key) or {}).get("selected_tests") or []
            for item in tests_selected:
                code = self._as_str(item.get("booked_code"))
                if not code or code in seen_codes or code in parent_codes:
                    continue
                seen_codes.add(code)
                tests_out.append({
                    "booked_code": code,
                    "test_name": self._as_str(item.get("description")) or self._as_str(item.get("test_name")) or code,
                    "test_status": 0,
                    "mrp": self._to_float(item.get("mrp")),
                    "charge": self._to_float(item.get("charge")),
                    "max_discount": self._to_float(item.get("max_discount")),
                })

            result[pid] = tests_out

        return result

    def get_my_assigned_bookings(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BookingSummary]:
        rows = self.repository.get_my_assigned_merged(
            user_id=user_id,
            status_filter=[0, 1, 2],
            include_terminal=False,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return [
            BookingSummary(
                id=row["id"],
                booking_status=row.get("booking_status"),
                preferred_visit_date=row.get("preferred_visit_date"),
                preferred_time_slot=self._as_str(row.get("preferred_time_slot")),
                patient_count=int(row.get("patient_count") or 0),
                source_type=row.get("source_type", "BOOKING"),
                patient_scope=row.get("patient_scope", "BOOKING_ALL_FALLBACK"),
                booking_id=row.get("booking_id"),
                appointment_id=row.get("appointment_id"),
                appointment_no=self._as_str(row.get("appointment_no")),
                caller_mobile=self._as_str(row.get("caller_mobile")),
                route=self._as_str(row.get("route")),
                patient_names=self._as_str(row.get("patient_names")),
            )
            for row in rows
        ]

    def get_my_assigned_history_bookings(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BookingSummary]:
        rows = self.repository.get_my_assigned_merged(
            user_id=user_id,
            status_filter=[3, 4, 5],
            include_terminal=True,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return [
            BookingSummary(
                id=row["id"],
                booking_status=row.get("booking_status"),
                preferred_visit_date=row.get("preferred_visit_date"),
                preferred_time_slot=self._as_str(row.get("preferred_time_slot")),
                patient_count=int(row.get("patient_count") or 0),
                source_type=row.get("source_type", "BOOKING"),
                patient_scope=row.get("patient_scope", "BOOKING_ALL_FALLBACK"),
                booking_id=row.get("booking_id"),
                appointment_id=row.get("appointment_id"),
                appointment_no=self._as_str(row.get("appointment_no")),
                caller_mobile=self._as_str(row.get("caller_mobile")),
                route=self._as_str(row.get("route")),
                patient_names=self._as_str(row.get("patient_names")),
            )
            for row in rows
        ]

    def get_my_assigned_booking_details(
        self,
        booking_id: int,
        user_id: int,
        exclude_cancelled: bool = True,
        appointment_id: int | None = None,
        catalog_db: Session | None = None,
    ) -> BookingDetailsResponse:
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
            appt_snapshot = self.repository.get_appointment_tests_snapshot(
                appointment_id=appointment_id,
                user_id=user_id,
            )
            tests_by_patient = self._build_tests_from_appointment_snapshot(
                appt_snapshot,
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
            selected_charge_modes,
            selected_panel_companies,
            additional_discount_amount,
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
                elif "_PHOTO_" in u:
                    dtype = "patient_photo"
                else:
                    dtype = "patient_document"
                url = f"/static/uploads/patient_documents/{n}"
                patient_documents.append({"file": n, "type": dtype, "url": url})
                patient_document_urls.append(url)
            prescription_urls = [
                f"/static/uploads/prescriptions/{name}"
                for name in prescription_files
            ]
            patient_items.append(
                PatientDetails(
                    id=patient.id,
                    booking_patient_id=int(booking_patient_id),
                    booking_patient_status=int(booking_patient_status or 0),
                    test_booking_status=self._as_str(test_booking_status),
                    title=patient.title,
                    full_name=patient.full_name,
                    gender=patient.gender,
                    age_years=patient.age_years,
                    date_of_birth=patient.date_of_birth,
                    contact_mobile=patient.contact_mobile,
                    alternate_mobile=patient.alternate_mobile,
                    panel_company=patient.panel_company,
                    card_no=self._as_str(getattr(patient, "card_no", None)),
                    panel_code=identity.get("panel_code") if identity else None,
                    panel_abarid=identity.get("panel_abarid") if identity else None,
                    selected_comp_cat_ids=self._as_str(selected_comp_cat_ids),
                    selected_charge_modes=self._as_str(selected_charge_modes),
                    selected_panel_companies=self._as_str(selected_panel_companies),
                    additional_discount_amount=self._to_float(additional_discount_amount),
                    tag=patient.tag,
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
                        date_of_birth=lp.get("date_of_birth"),
                        contact_mobile=self._as_str(lp.get("contact_mobile")),
                        alternate_mobile=self._as_str(lp.get("alternate_mobile")),
                        panel_company=self._as_str(lp.get("panel_company")),
                        tag=self._as_str(lp.get("tag")),
                    )
                )

        return BookingDetailsResponse(
            booking_status=status_for_response,
            source_type="APPOINTMENT" if appointment_id is not None else "BOOKING",
            appointment_id=int(appointment_id) if appointment_id is not None else None,
            patient_scope=patient_scope,
            address=AddressDetails.model_validate(address) if address else None,
            patients=patient_items,
            linked_patients=linked_patient_items,
            F_Apt_Am=float(getattr(booking, 'F_Apt_Am', 0) or 0),
            F_dis=float(getattr(booking, 'F_dis', 0) or 0),
            Ad_Dis=float(getattr(booking, 'Ad_Dis', 0) or 0),
            total_amount=float(getattr(booking, 'total_amount', 0) or 0),
            referred_by=self._as_str(getattr(booking, 'referred_by', None)),
            intrnl_rfrncd_by=self._as_str(getattr(booking, 'intrnl_rfrncd_by', None)),
        )

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
        global_completion_lock_acquired = False

        try:
            if normalized_action == "complete" and appointment_id is None:
                global_completion_lock_acquired = self.repository.acquire_global_completion_lock(wait_timeout_sec=180)
                if not global_completion_lock_acquired:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Another booking completion is in progress. Please retry shortly",
                    )
                completion_lock_acquired = self.repository.acquire_booking_completion_lock(booking.id, wait_timeout_sec=120)

            if normalized_action == "complete" and payload is not None:
                incoming_tests = getattr(payload, "tests_payload", None)
                if incoming_tests and appointment_id is None:
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
                # Save only deselected/pending child tests for this booking context.
                self.repository.replace_pending_child_tests_for_booking(
                    booking_id=booking.id,
                    pending_rows=getattr(payload, "pending_child_tests", None) or [],
                )
                # Persist patient-level completion fields (APK_TBS/report/payment/pricks/sample collection/cancel metadata).
                patient_updates = getattr(payload, "patient_updates", None) or []
                if patient_updates:
                    self.repository.apply_completion_patient_updates(
                        booking_id=booking.id,
                        updates=patient_updates,
                        actor_user_id=user_id,
                    )
                    self.repository.handle_cancelled_patient_reschedule(
                        booking_id=booking.id,
                        updates=patient_updates,
                        actor_user_id=user_id,
                    )

                if patient_documents_map:
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
                        patient_doc_files: list = []
                        patient_doc_types: list[str] = []
                        manual_hcb_files: list = []
                        for idx, f in enumerate(files):
                            dtype = doc_types[idx] if idx < len(doc_types) else ""
                            if dtype in {"cghs_card", "patient_photo"}:
                                patient_doc_files.append(f)
                                patient_doc_types.append(dtype)
                            elif dtype == "prescription":
                                prescription_files.append(f)
                            elif self._is_manual_hcb_slip(dtype):
                                manual_hcb_files.append(f)
                            else:
                                # Backward compatible fallback for legacy payloads.
                                prescription_files.append(f)

                        existing_prescriptions = self.repository.get_patient_prescription_paths(booking_id=booking.id, patient_id=patient_id)
                        existing_patient_docs = self.repository.get_patient_document_paths(patient_id=patient_id)
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
                            self.repository.set_patient_payment_screenshots(
                                booking_id=booking.id,
                                patient_id=patient_id,
                                rel_paths=paths,
                            )

                followup_required = bool(getattr(payload, "followup_required", False))
                pending_child_rows = (getattr(payload, "pending_child_tests", None) or [])
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
                    for row in pending_child_rows:
                        r = row or {}
                        pid = int(r.get("patient_id") or 0)
                        if pid <= 0:
                            continue
                        key = str(pid)
                        tbs_value = tbs_by_pid.get(pid)
                        root_code = str(r.get("root_booked_code") or "").strip()
                        root_name = str(r.get("root_test_name") or root_code).strip()
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
                        if root_code:
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
                            tests_billing_map[key]["selected_tests"].append({
                                "booked_code": root_code,
                                "description": root_name,
                                "charge": 0,
                                "mrp": 0,
                                "max_discount": 0,
                                "max_allowed_discount": 0,
                            })
                            tests_billing_map[key]["panels"][0]["selected_tests"].append({
                                "booked_code": root_code,
                                "description": root_name,
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
                                "panels": [{
                                    "panel": {"pname": ""},
                                    "billing": {"comp_cat_id": "", "selected_charge_mode": ""},
                                    "selected_tests": [],
                                }],
                                "cce_level_tbs": tbs_value,
                            })
                            if pending_tests_map[key].get("cce_level_tbs") in (None, "") and tbs_value not in (None, ""):
                                pending_tests_map[key]["cce_level_tbs"] = tbs_value
                            pending_tests_map[key]["selected_tests"].extend(child_rows)
                            pending_tests_map[key]["panels"][0]["selected_tests"].extend(child_rows)

                    snapshot_payload = {
                        "tests_billing_map": tests_billing_map,
                        "pending_tests_map": pending_tests_map,
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
                final_status, patient_rows, patient_scope = self.repository.apply_appointment_action(
                    booking_id=booking.id,
                    appointment_id=appointment_id,
                    user_id=user_id,
                    action=normalized_action,
                )
                source_type = "APPOINTMENT"
                detail = f"Appointment action '{normalized_action}' applied successfully"
            else:
                if normalized_action == "complete" and payload is not None and (getattr(payload, "patient_updates", None) or []):
                    final_status, patient_rows = self.repository.apply_booking_completion_patientwise(
                        booking_id=booking.id,
                        updates=(getattr(payload, "patient_updates", None) or []),
                        actor_user_id=user_id,
                    )
                else:
                    final_status, patient_rows = self.repository.apply_booking_action(
                        booking_id=booking.id,
                        action=normalized_action,
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
            if global_completion_lock_acquired:
                self.repository.release_global_completion_lock()

        return BookingStatusUpdateResponse(
            booking_id=booking.id,
            booking_status=final_status,
            action=action,
            patients=patient_rows,
            detail=detail,
            source_type=source_type,
            appointment_id=appointment_id,
            patient_scope=patient_scope,
        )
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
    ) -> dict:
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
            if appointment_id is not None and int(appointment_id or 0) > 0:
                status_code, _patient_rows, _scope = self.repository.apply_appointment_action(
                    booking_id=booking.id,
                    appointment_id=int(appointment_id),
                    user_id=user_id,
                    action="cancel",
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


    def _mirror_upload_roots(self, kind: str) -> list[Path]:
        local_root = Path(__file__).resolve().parents[2]
        web_root = Path(r"C:\Users\user\Desktop\lead_capture_project_mainss\lead_capture_project_main")
        if kind == "patient_documents":
            rel = Path("app") / "static" / "uploads" / "patient_documents"
            roots = [local_root / rel, web_root / rel]
        elif kind == "prescriptions":
            rel = Path("app") / "static" / "uploads" / "prescriptions"
            roots = [local_root / rel, web_root / rel]
        elif kind == "payment_shot":
            rel = Path("app") / "static" / "uploads" / "payment_shot"
            roots = [web_root / rel]
        else:
            rel = Path("app") / "static" / "uploads"
            roots = [local_root / rel, web_root / rel]
        uniq = []
        seen = set()
        for root in roots:
            key = str(root.resolve()) if root.exists() else str(root)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(root)
        return uniq

    def _mirror_saved_upload(self, kind: str, relative_path: str, src_file: Path) -> None:
        rel = Path(relative_path.replace("\\", "/"))
        for root in self._mirror_upload_roots(kind):
            dst = root / rel
            try:
                if dst.resolve() == src_file.resolve():
                    continue
            except Exception:
                pass
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src_file.read_bytes())
            except Exception:
                pass

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
            self._mirror_saved_upload("patient_documents", rel_saved, out_path)

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
            self._mirror_saved_upload("prescriptions", rel_saved, out_path)

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
        web_root = Path(r"C:\Users\user\Desktop\lead_capture_project_mainss\lead_capture_project_main")
        base_dir = web_root / "app" / "static" / "uploads" / "hc_slip" / clean_booking_code / f"PT{int(patient_id)}"
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
        web_root = Path(r"C:\Users\user\Desktop\lead_capture_project_mainss\lead_capture_project_main")
        folder = str(category or "").strip().lower()
        if folder:
            base_dir = web_root / "app" / "static" / "uploads" / "payment_shot" / folder / clean_booking_code / f"PT{int(patient_id)}"
        else:
            base_dir = web_root / "app" / "static" / "uploads" / "payment_shot" / clean_booking_code / f"PT{int(patient_id)}"
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
                    batch=batch if isinstance(batch, dict) else {},
                    booking_ids=[int(x) for x in (booking_ids or []) if str(x).strip().isdigit()],
                    patients=patients if isinstance(patients, list) else [],
                    tubes=tubes if isinstance(tubes, list) else [],
                    created_at=str(created_at) if created_at is not None else None,
                )
            )

        return BatchListResponse(items=items)

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
            booking_ids.append(bid)
            for p in (b.patients or []):
                pid = int(p.patient_id)
                bpid = int(p.booking_patient_id)
                pname = (p.patient_name or "").strip() or None
                patients_rows.append({
                    "booking_id": bid,
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

