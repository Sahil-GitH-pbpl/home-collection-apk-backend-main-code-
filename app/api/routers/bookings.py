import json

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import ClientDisconnect

from app.api.dependencies import get_current_user
from app.core.database import get_catalog_db, get_db
from app.models.user import User
from app.repositories.booking_repository import BookingRepository
from app.schemas.booking import (
    AddPatientToBookingRequest,
    AddPatientToBookingResponse,
    BookingCancelRequest,
    BookingCancelResponse,
    BookingDetailsResponse,
    BookingStatusUpdateRequest,
    BookingStatusUpdateResponse,
    BookingSummary,
    EditPatientInBookingRequest,
    EditPatientInBookingResponse,
    EditBookingAddressRequest,
    EditBookingAddressResponse,
    BatchReadyResponse,
    BatchSaveRequest,
    BatchSaveResponse,
    BatchListResponse,
)
from app.services.booking_service import BookingService

router = APIRouter(prefix="/api/v1/bookings", tags=["Bookings"])
logger = logging.getLogger("uvicorn.error")


def _client_ip(request: Request) -> str:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else ""


def _user_label(user: User) -> str:
    return f"{user.id}:{user.name}"


def _clean_text(value) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _snapshot_test_names(node) -> list[str]:
    sections = (node or {}).get("panels") if isinstance(node, dict) else None
    sections = sections if isinstance(sections, list) and sections else [node or {}]
    out: list[str] = []
    seen: set[str] = set()
    for sec in sections:
        for item in ((sec or {}).get("selected_tests") or []):
            name = _clean_text((item or {}).get("description") or (item or {}).get("test_name") or (item or {}).get("booked_code"))
            key = (name or "").lower()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
    return out


@router.get("/my-assigned", response_model=list[BookingSummary])
def get_my_assigned_bookings(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BookingSummary]:
    service = BookingService(repository=BookingRepository(db))
    return service.get_my_assigned_bookings(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )


@router.get("/my-assigned/history", response_model=list[BookingSummary])
def get_my_assigned_history_bookings(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BookingSummary]:
    service = BookingService(repository=BookingRepository(db))
    return service.get_my_assigned_history_bookings(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )


@router.get("/my-assigned/history/detail")
def get_my_assigned_history_detail(
    source_type: str = "BOOKING",
    booking_id: int = 0,
    appointment_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    src = (source_type or "BOOKING").strip().upper()
    if src not in {"BOOKING", "APPOINTMENT"}:
        raise HTTPException(status_code=400, detail="source_type must be BOOKING or APPOINTMENT")
    if booking_id <= 0:
        raise HTTPException(status_code=400, detail="booking_id is required")

    selected_ids: set[int] | None = None
    completed: dict[int, list[str]] = {}
    cancelled: dict[int, list[str]] = {}
    appt_pay: dict[int, dict] = {}
    if src == "APPOINTMENT":
        if not appointment_id:
            raise HTTPException(status_code=400, detail="appointment_id is required for APPOINTMENT")
        appt = db.execute(
            text(
                """
                SELECT booking_id, appointment_status, selected_patient_ids_json,
                       appointment_tests_snapshot_json, payment_snapshot_json
                FROM hhome_collection_booking_appointment
                WHERE id=:appointment_id
                  AND booking_id=:booking_id
                  AND assigned_phlebotomist_id=:user_id
                  AND COALESCE(appointment_status, 0) IN (3, 4, 5)
                LIMIT 1
                """
            ),
            {"appointment_id": int(appointment_id), "booking_id": int(booking_id), "user_id": int(current_user.id)},
        ).mappings().first()
        if not appt:
            raise HTTPException(status_code=404, detail="Completed appointment not found")
        try:
            selected_ids = {int(x) for x in json.loads(appt.get("selected_patient_ids_json") or "[]") if str(x).isdigit()}
        except Exception:
            selected_ids = set()
        try:
            snap = json.loads(appt.get("appointment_tests_snapshot_json") or "{}")
        except Exception:
            snap = {}
        for key, node in ((snap.get("tests_billing_map") or {}) if isinstance(snap, dict) else {}).items():
            if str(key).isdigit():
                completed[int(key)] = _snapshot_test_names(node)

        try:
            pay_snap = json.loads(appt.get("payment_snapshot_json") or "{}")
        except Exception:
            pay_snap = {}
        for row in (pay_snap.get("payments") or []):
            if isinstance(row, dict) and str(row.get("patient_id") or "").isdigit():
                appt_pay[int(row.get("patient_id"))] = row
        status_value = appt.get("appointment_status")
    else:
        booking = db.execute(
            text(
                """
                SELECT id, booking_status
                FROM hhome_collection_booking
                WHERE id=:booking_id
                  AND assigned_phlebotomist_id=:user_id
                  AND booking_status IN (3, 4, 5)
                LIMIT 1
                """
            ),
            {"booking_id": int(booking_id), "user_id": int(current_user.id)},
        ).mappings().first()
        if not booking:
            raise HTTPException(status_code=404, detail="Completed booking not found")
        status_value = booking.get("booking_status")
        for row in db.execute(
            text(
                """
                SELECT patient_id, test_name, booked_code, test_status
                FROM hhome_collection_booking_patient_test
                WHERE booking_id=:booking_id
                ORDER BY id ASC
                """
            ),
            {"booking_id": int(booking_id)},
        ).mappings():
            pid = int(row.get("patient_id") or 0)
            name = _clean_text(row.get("test_name") or row.get("booked_code"))
            if not pid or not name:
                continue
            bucket = cancelled if str(row.get("test_status") or "").strip().lower() in {"2", "dropped", "cancelled", "canceled"} else completed
            bucket.setdefault(pid, [])
            if name not in bucket[pid]:
                bucket[pid].append(name)

    patient_rows = db.execute(
        text(
            """
        SELECT
            bp.id AS booking_patient_id,
            bp.patient_id,
            bp.booking_patient_status,
            bp.APK_TBS AS apk_tbs,
            bp.ref_by,
            bp.report_delivery,
            bp.report_schedule,
            bp.payment_mode,
            bp.payment_amount,
            TRIM(CONCAT(COALESCE(p.title, ''), ' ', COALESCE(p.full_name, ''))) AS patient_name
        FROM hhome_collection_booking_patient bp
        LEFT JOIN hpatient_master p ON p.id = bp.patient_id
        WHERE bp.booking_id=:booking_id
        ORDER BY bp.id ASC
            """
        ),
        {"booking_id": int(booking_id)},
    ).mappings().all()

    patients = []
    for row in patient_rows:
        pid = int(row.get("patient_id") or 0)
        if selected_ids is not None and selected_ids and pid not in selected_ids:
            continue
        pay = appt_pay.get(pid) if src == "APPOINTMENT" else None
        patient_status = int(row.get("booking_patient_status") or 0)
        patient_completed = list(completed.get(pid, []))
        patient_cancelled = list(cancelled.get(pid, []))
        if patient_status == 4:
            merged_cancelled: list[str] = []
            for name in patient_cancelled + patient_completed:
                if name not in merged_cancelled:
                    merged_cancelled.append(name)
            patient_completed = []
            patient_cancelled = merged_cancelled
        patients.append(
            {
                "patient_name": _clean_text(row.get("patient_name")),
                "booking_patient_status": patient_status,
                "apk_tbs": _clean_text(row.get("apk_tbs")),
                "ref_by": _clean_text(row.get("ref_by")),
                "report_delivery": _clean_text(row.get("report_delivery")),
                "report_schedule": _clean_text(row.get("report_schedule")),
                "payment_mode": _clean_text((pay or {}).get("payment_mode") if pay else row.get("payment_mode")),
                "payment_amount": _to_float((pay or {}).get("payment_amount") if pay else row.get("payment_amount")),
                "completed_tests": patient_completed,
                "cancelled_tests": patient_cancelled,
            }
        )

    return {
        "source_type": src,
        "booking_id": int(booking_id),
        "appointment_id": int(appointment_id) if src == "APPOINTMENT" and appointment_id else None,
        "booking_status": int(status_value or 0),
        "patients": patients,
    }


@router.get("/my-assigned/{booking_id}", response_model=BookingDetailsResponse)
def get_my_assigned_booking_details(
    booking_id: int,
    request: Request,
    appointment_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    catalog_db: Session = Depends(get_catalog_db),
) -> BookingDetailsResponse:
    logger.info(
        "assigned_booking_detail_request user=%s booking_id=%s appointment_id=%s client_ip=%s",
        _user_label(current_user),
        booking_id,
        appointment_id,
        _client_ip(request),
    )
    service = BookingService(repository=BookingRepository(db))
    return service.get_my_assigned_booking_details(
        booking_id=booking_id,
        user_id=current_user.id,
        exclude_cancelled=False,
        appointment_id=appointment_id,
        catalog_db=catalog_db,
    )




@router.post(
    "/my-assigned/{booking_id}/cancel",
    response_model=BookingCancelResponse,
)
def cancel_my_assigned_booking_direct(
    booking_id: int,
    payload: BookingCancelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingCancelResponse:
    service = BookingService(repository=BookingRepository(db))
    result = service.cancel_assigned_booking_direct(
        booking_id=booking_id,
        user_id=current_user.id,
        reason_text=payload.reason_text,
        remark=payload.remark,
        complete_time=payload.complete_time,
        complete_location=payload.complete_location,
        reschedule_requested=payload.reschedule_requested,
        proposed_visit_date=(payload.proposed_visit_date.isoformat() if payload.proposed_visit_date else None),
        proposed_time_slot=payload.proposed_time_slot,
        appointment_id=payload.appointment_id,
    )
    return BookingCancelResponse(**result)

@router.post(
    "/my-assigned/{booking_id}/status",
    response_model=BookingStatusUpdateResponse,
)
async def update_my_assigned_booking_status(
    booking_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    catalog_db: Session = Depends(get_catalog_db),
) -> BookingStatusUpdateResponse:
    service = BookingService(repository=BookingRepository(db))
    content_type = (request.headers.get("content-type") or "").lower()
    patient_documents_map: dict[int, list[StarletteUploadFile]] = {}
    payment_screenshots_map: dict[int, list[StarletteUploadFile]] = {}

    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except ClientDisconnect as exc:
            raise HTTPException(status_code=499, detail="Client disconnected during upload") from exc
        payload_raw = form.get("payload") or form.get("data") or form.get("body")
        if not payload_raw:
            raise HTTPException(status_code=422, detail="Missing payload in multipart request")
        try:
            payload_data = json.loads(str(payload_raw))
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Invalid payload JSON in multipart request") from exc

        for key, val in form.multi_items():
            if not isinstance(val, StarletteUploadFile):
                continue
            k = str(key)
            if k.startswith("patient_documents_"):
                raw_pid = k.replace("patient_documents_", "", 1).strip()
                try:
                    pid = int(raw_pid)
                except Exception:
                    continue
                patient_documents_map.setdefault(pid, []).append(val)
                continue
            if k.startswith("payment_shot_"):
                raw_pid = k.replace("payment_shot_", "", 1).strip()
                try:
                    pid = int(raw_pid)
                except Exception:
                    continue
                payment_screenshots_map.setdefault(pid, []).append(val)
                continue
            continue
    else:
        payload_data = await request.json()

    try:
        payload = BookingStatusUpdateRequest.model_validate(payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    logger.info(
        "assigned_booking_status_request user=%s booking_id=%s appointment_id=%s action=%s client_ip=%s content_type=%s",
        _user_label(current_user),
        booking_id,
        payload.appointment_id,
        payload.action,
        _client_ip(request),
        content_type or "application/json",
    )

    return service.update_assigned_booking_status(
        booking_id=booking_id,
        user_id=current_user.id,
        action=payload.action,
        appointment_id=payload.appointment_id,
        payload=payload,
        catalog_db=catalog_db,
        patient_documents_map=patient_documents_map,
        payment_screenshots_map=payment_screenshots_map,
    )




@router.get(
    "/my-assigned/batch/ready",
    response_model=BatchReadyResponse,
)
def get_my_assigned_batch_ready(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BatchReadyResponse:
    service = BookingService(repository=BookingRepository(db))
    return service.get_my_batch_ready_bookings(user_id=current_user.id)


@router.get(
    "/my-assigned/batch/history",
    response_model=BatchListResponse,
)
def get_my_assigned_batch_history(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BatchListResponse:
    service = BookingService(repository=BookingRepository(db))
    return service.get_my_batch_handover_history(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )

@router.post(
    "/my-assigned/batch/save",
    response_model=BatchSaveResponse,
)
def save_my_assigned_batch(
    payload: BatchSaveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BatchSaveResponse:
    service = BookingService(repository=BookingRepository(db))
    return service.save_batch_handover(
        user_id=current_user.id,
        payload=payload,
    )


@router.post(
    "/my-assigned/{booking_id}/patients",
    response_model=AddPatientToBookingResponse,
)
async def add_patient_to_existing_booking(
    booking_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AddPatientToBookingResponse:
    service = BookingService(repository=BookingRepository(db))
    content_type = (request.headers.get("content-type") or "").lower()
    files: list[StarletteUploadFile] | None = None
    payload_data: dict

    def _none_if_blank(value):
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except ClientDisconnect as exc:
            raise HTTPException(status_code=499, detail="Client disconnected during upload") from exc
        form_files = form.getlist("patient_documents")
        files = [f for f in form_files if isinstance(f, StarletteUploadFile)]
        payload_data = {
            "title": _none_if_blank(form.get("title")),
            "full_name": _none_if_blank(form.get("full_name")),
            "gender": _none_if_blank(form.get("gender")),
            "date_of_birth": _none_if_blank(form.get("date_of_birth")),
            "age_years": _none_if_blank(form.get("age_years")),
            "primary_mobile": _none_if_blank(form.get("contact_mobile") or form.get("primary_mobile")),
            "alternate_mobile": _none_if_blank(form.get("alternate_mobile")),
            "email": _none_if_blank(form.get("email")),
            "labmate_pid": _none_if_blank(form.get("labmate_pid")),
            "panel_company": _none_if_blank(form.get("panel_company")),
            "tag": _none_if_blank(form.get("tag")),
            "card_number": _none_if_blank(form.get("card_number")),
        }
    else:
        payload_data = await request.json()

    try:
        payload = AddPatientToBookingRequest.model_validate(payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    return service.add_patient_to_existing_booking(
        booking_id=booking_id,
        user_id=current_user.id,
        payload=payload,
        patient_documents=files,
    )


@router.put(
    "/my-assigned/{booking_id}/patients/{patient_id}",
    response_model=EditPatientInBookingResponse,
)
async def edit_patient_in_existing_booking(
    booking_id: int,
    patient_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EditPatientInBookingResponse:
    service = BookingService(repository=BookingRepository(db))
    content_type = (request.headers.get("content-type") or "").lower()
    files: list[StarletteUploadFile] | None = None

    def _none_if_blank(value):
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except ClientDisconnect as exc:
            raise HTTPException(status_code=499, detail="Client disconnected during upload") from exc
        form_files = form.getlist("patient_documents")
        files = [f for f in form_files if isinstance(f, StarletteUploadFile)]
        payload_data = {
            "title": _none_if_blank(form.get("title")),
            "full_name": _none_if_blank(form.get("full_name")),
            "gender": _none_if_blank(form.get("gender")),
            "date_of_birth": _none_if_blank(form.get("date_of_birth")),
            "age_years": _none_if_blank(form.get("age_years")),
            "primary_mobile": _none_if_blank(form.get("primary_mobile") or form.get("contact_mobile")),
            "alternate_mobile": _none_if_blank(form.get("alternate_mobile")),
            "labmate_pid": _none_if_blank(form.get("labmate_pid")),
            "panel_company": _none_if_blank(form.get("panel_company")),
            "tag": _none_if_blank(form.get("tag")),
        }
    else:
        payload_data = await request.json()

    try:
        payload = EditPatientInBookingRequest.model_validate(payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    return service.edit_patient_in_existing_booking(
        booking_id=booking_id,
        patient_id=patient_id,
        user_id=current_user.id,
        payload=payload,
        patient_documents=files,
    )




@router.put(
    "/my-assigned/{booking_id}/address",
    response_model=EditBookingAddressResponse,
)
async def edit_booking_address(
    booking_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EditBookingAddressResponse:
    service = BookingService(repository=BookingRepository(db))
    payload_data = await request.json()
    try:
        payload = EditBookingAddressRequest.model_validate(payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    return service.edit_booking_address(
        booking_id=booking_id,
        user_id=current_user.id,
        payload=payload,
    )
