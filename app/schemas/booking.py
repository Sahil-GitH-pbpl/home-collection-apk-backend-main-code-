from datetime import date
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator


class BookingSummary(BaseModel):
    id: int
    booking_status: int | None = None
    preferred_visit_date: date | None = None
    preferred_time_slot: str | None = None
    source_type: Literal["BOOKING", "APPOINTMENT"] = "BOOKING"
    patient_scope: Literal["APPOINTMENT_SELECTED", "BOOKING_ALL_FALLBACK"] = "BOOKING_ALL_FALLBACK"
    booking_id: int | None = None
    appointment_id: int | None = None
    appointment_no: str | None = None
    caller_mobile: str | None = None
    route: str | None = None
    patient_names: list[str] = Field(default_factory=list)


class AddressDetails(BaseModel):
    id: int
    address_type: str | None = None
    house_flat_no: str | None = None
    floor: str | None = None
    street_line: str | None = None
    landmark: str | None = None
    colony_name_snapshot: str | None = None
    pincode_snapshot: str | None = None
    pincode: str | None = None
    route_no_snapshot: str | None = None
    location_url: str | None = None
    city: str | None = None
    access_notes: str | None = None


class BookingTest(BaseModel):
    booked_code: str | None = None
    comp_cat_id: str | None = None
    panel_company: str | None = None
    test_name: str | None = None
    test_status: int | None = None
    mrp: float | None = None
    charge: float | None = None
    max_discount: float | None = None


class BookingAmounts(BaseModel):
    subtotal: float = 0
    base_discount: float = 0
    additional: float = 0
    final_discount: float = 0
    final_amount: float = 0


class PatientDetails(BaseModel):
    id: int
    booking_patient_id: int
    booking_patient_status: int | None = None
    test_booking_status: str | None = None
    title: str | None = None
    full_name: str | None = None
    gender: str | None = None
    age_years: int | None = None
    date_of_birth: date | None = None
    contact_mobile: str | None = None
    alternate_mobile: str | None = None
    panel_company: str | None = None
    card_no: str | None = None
    panel_code: str | None = None
    panel_abarid: str | None = None
    selected_comp_cat_ids: str | None = None
    referred_by: str | None = None
    selected_charge_modes: str | None = None
    selected_panel_companies: str | None = None
    additional_discount_amount: float | None = None
    appointment_patient_status: int | None = None
    booking_due_amount: float | None = None
    booking_extra_amount: float | None = None
    booking_payment_mode: str | None = None
    tag: str | None = None
    patient_documents: list[dict] = Field(default_factory=list)
    patient_document_urls: list[str] = Field(default_factory=list)
    prescription_files: list[str] = Field(default_factory=list)
    prescription_urls: list[str] = Field(default_factory=list)
    tests: list[BookingTest]


class LinkedPatientDetails(BaseModel):
    id: int
    patient_code: str | None = None
    title: str | None = None
    full_name: str | None = None
    gender: str | None = None
    age_years: int | None = None
    date_of_birth: date | None = None
    contact_mobile: str | None = None
    alternate_mobile: str | None = None
    panel_company: str | None = None
    tag: str | None = None


class BookingDetailsResponse(BaseModel):
    booking_status: int | None = None
    source_type: Literal["BOOKING", "APPOINTMENT"] = "BOOKING"
    appointment_id: int | None = None
    patient_scope: Literal["APPOINTMENT_SELECTED", "BOOKING_ALL_FALLBACK"] = "BOOKING_ALL_FALLBACK"
    address: AddressDetails | None = None
    patients: list[PatientDetails]
    linked_patients: list[LinkedPatientDetails] = Field(default_factory=list)
    F_Apt_Am: float | None = None
    F_dis: float | None = None
    Ad_Dis: float | None = None
    total_amount: float | None = None
    intrnl_rfrncd_by: str | None = None


class BookingStatusUpdateRequest(BaseModel):
    action: Literal["assign", "start", "stop", "complete", "completed"]
    appointment_id: int | None = None
    start_time: str | None = None
    start_location: str | None = None
    complete_time: str | None = None
    complete_location: str | None = None
    additional_discount_mode: str | None = None
    additional_discount_value: float | None = None
    tests_payload: list[dict] | None = None
    pending_child_tests: list[dict] | None = None
    patient_updates: list[dict] | None = None
    followup_required: bool | None = None
    followup_date: date | None = None
    followup_time_slot: str | None = None
    followup_created_by: int | None = None


class BookingPatientStatusItem(BaseModel):
    booking_patient_id: int
    patient_id: int
    booking_patient_status: int


class BookingStatusUpdateResponse(BaseModel):
    booking_id: int
    booking_status: int
    action: Literal["assign", "start", "stop", "complete", "completed"]
    patients: list[BookingPatientStatusItem]
    detail: str
    source_type: Literal["BOOKING", "APPOINTMENT"] = "BOOKING"
    appointment_id: int | None = None
    patient_scope: Literal["APPOINTMENT_SELECTED", "BOOKING_ALL_FALLBACK"] = "BOOKING_ALL_FALLBACK"




class BookingCancelRequest(BaseModel):
    appointment_id: int | None = None
    reason_text: str
    remark: str | None = None
    complete_time: str | None = None
    complete_location: str | None = None
    reschedule_requested: bool = False
    proposed_visit_date: date | None = None
    proposed_time_slot: str | None = None


class BookingCancelResponse(BaseModel):
    ok: bool = True
    booking_id: int
    booking_status: int
    lead_created: bool = False
    lead_id: str | None = None
    detail: str

class AddPatientToBookingRequest(BaseModel):
    existing_patient_id: int | None = None
    title: str | None = None
    full_name: str | None = None
    gender: str | None = None
    date_of_birth: date | None = None
    age_years: int | None = None
    primary_mobile: str | None = None
    alternate_mobile: str | None = None
    email: str | None = None
    labmate_pid: str | None = None
    panel_company: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def _validate_add_patient_mode(self):
        if self.existing_patient_id is not None:
            return self
        missing = []
        if not self.full_name:
            missing.append("full_name")
        if not self.gender:
            missing.append("gender")
        if not self.primary_mobile:
            missing.append("primary_mobile")
        if missing:
            raise ValueError(
                f"Missing required field(s) for new patient: {', '.join(missing)}"
            )
        return self


class AddPatientToBookingResponse(BaseModel):
    ok: bool = True
    booking_id: int
    patient_id: int
    patient_code: str
    booking_patient_id: int
    linked_to_booking: bool = True
    linked_mobiles: list[str]
    message: str
    uploaded_documents: list[str] = Field(default_factory=list)
    uploaded_documents_count: int = 0


class EditPatientInBookingRequest(BaseModel):
    title: str | None = None
    full_name: str | None = None
    gender: str | None = None
    date_of_birth: date | None = None
    age_years: int | None = None
    primary_mobile: str | None = None
    alternate_mobile: str | None = None
    labmate_pid: str | None = None
    panel_company: str | None = None
    tag: str | None = None


class EditPatientInBookingResponse(BaseModel):
    ok: bool = True
    booking_id: int
    patient_id: int
    patient_code: str
    linked_mobiles: list[str]
    message: str


class EditBookingAddressRequest(BaseModel):
    address_id: int | None = None
    address_type: str | None = None
    house_flat_no: str | None = None
    floor: str | None = None
    street_line: str | None = None
    landmark: str | None = None
    colony_name: str | None = None
    pincode: str | None = None
    route_no: str | None = None
    city: str | None = None
    google_location: str | None = None
    access_notes: str | None = None


class EditBookingAddressResponse(BaseModel):
    ok: bool = True
    booking_id: int
    address_id: int
    message: str
    address: AddressDetails | None = None


class MobileSelectedTest(BaseModel):
    booked_code: str
    description: str | None = None
    mrp: float = 0
    charge: float = 0
    max_discount: float = 0
    max_allowed_discount: float = 0


class MobilePanelPayload(BaseModel):
    panel_company: str | None = None
    comp_cat_id: str | None = None
    selected_charge_mode: str | None = None
    selected_tests: list[MobileSelectedTest] = Field(default_factory=list)


class MobilePatientTestsPayload(BaseModel):
    patient_id: int
    panels: list[MobilePanelPayload] = Field(default_factory=list)


class MobileBookingTestsSaveRequest(BaseModel):
    additional_discount_mode: str | None = None
    additional_discount_value: float = 0
    tests_payload: list[MobilePatientTestsPayload] = Field(default_factory=list)


class MobileBookingTestsSaveResponse(BaseModel):
    ok: bool = True
    booking_id: int
    saved_amounts: BookingAmounts
    active_tests_count: int
    dropped_tests_count: int













class BatchTubeItem(BaseModel):
    tube_name: str


class BatchPatientItem(BaseModel):
    patient_id: int
    booking_patient_id: int
    patient_name: str | None = None
    tubes: list[BatchTubeItem] = Field(default_factory=list)


class BatchBookingItem(BaseModel):
    booking_id: int
    appointment_id: int | None = None
    booking_code: str | None = None
    patients: list[BatchPatientItem] = Field(default_factory=list)


class BatchMetaPayload(BaseModel):
    handover_to: str | None = None
    rider_name: str | None = None
    handed_over_at: str | None = None
    booking_count: int | None = None
    tube_count: int | None = None


class BatchSaveRequest(BaseModel):
    batch: BatchMetaPayload
    bookings: list[BatchBookingItem] = Field(default_factory=list)


class BatchSaveResponse(BaseModel):
    ok: bool = True
    batch_id: int
    detail: str


class BatchListItem(BaseModel):
    id: int
    batch_code: str | None = None
    batch: dict = Field(default_factory=dict)
    booking_ids: list[int] = Field(default_factory=list)
    patients: list[dict] = Field(default_factory=list)
    tubes: list[dict] = Field(default_factory=list)
    created_at: str | None = None


class BatchListResponse(BaseModel):
    items: list[BatchListItem] = Field(default_factory=list)


