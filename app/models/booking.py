from datetime import date
from typing import Optional

from sqlalchemy import Date, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HomeCollectionBooking(Base):
    __tablename__ = "hhome_collection_booking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    booking_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    caller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    selected_address_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    address_snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    preferred_visit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    preferred_time_slot: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    booking_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    assigned_phlebotomist_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    F_Apt_Am: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    F_dis: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    Ad_Dis: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    referred_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    intrnl_rfrncd_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    booking_tags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    bkg_ref_flag: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class HomeCollectionBookingPatient(Base):
    __tablename__ = "hhome_collection_booking_patient"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    patient_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    booking_patient_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cce_level_TBS: Mapped[Optional[str]] = mapped_column("cce_level_TBS", String(120), nullable=True)
    selected_comp_cat_ids: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    referred_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    selected_charge_modes: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    selected_panel_companies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    patient_final_amount: Mapped[Optional[float]] = mapped_column("patient_final_amount", Numeric(12, 2), nullable=True)
    prescription_files: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    patient_photo_files: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_mode: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    due_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    extra_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    additional_discount_amount: Mapped[Optional[float]] = mapped_column("additional_discount_amount", Numeric(12, 2), nullable=True)


class HomeCollectionBookingPatientTest(Base):
    __tablename__ = "hhome_collection_booking_patient_test"

    booking_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booked_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    test_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class CallerMaster(Base):
    __tablename__ = "hcaller_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caller_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    primary_mobile: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    alternate_mobile: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class PatientMaster(Base):
    __tablename__ = "hpatient_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    age_years: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    contact_mobile: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    alternate_mobile: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    panel_company: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    card_no: Mapped[Optional[str]] = mapped_column("card_number", String(100), nullable=True)
    tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    patient_documents: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AddressMaster(Base):
    __tablename__ = "haddress_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    house_flat_no: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    floor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    street_line: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    landmark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    colony_name_snapshot: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pincode_snapshot: Mapped[Optional[str]] = mapped_column("pincode", String(20), nullable=True)
    location_url: Mapped[Optional[str]] = mapped_column("google_location", Text, nullable=True)
    route_no_snapshot: Mapped[Optional[str]] = mapped_column("route_no", String(100), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    access_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)







