import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

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
    BatchSaveRequest,
    BatchSaveResponse,
    BatchListResponse,
)
from app.services.booking_service import BookingService

router = APIRouter(prefix="/api/v1/bookings", tags=["Bookings"])


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


@router.get("/my-assigned/{booking_id}", response_model=BookingDetailsResponse)
def get_my_assigned_booking_details(
    booking_id: int,
    appointment_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    catalog_db: Session = Depends(get_catalog_db),
) -> BookingDetailsResponse:
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
        form = await request.form()
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
        form = await request.form()
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
        form = await request.form()
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
