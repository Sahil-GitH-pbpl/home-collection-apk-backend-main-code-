# Home Collection Mobile API (FastAPI)

Mobile-only Home Collection API using FastAPI + MySQL + JWT.

## Scope Applied

- Only Home Collection app APIs.
- Only one DB allowed: `lead_management`.
- No tube mapping logic.
- No child test expansion logic.
- Tests are returned exactly from `hhome_collection_booking_patient_test`.

## Tech Stack

- FastAPI
- SQLAlchemy + PyMySQL
- JWT Bearer auth (`python-jose`)
- Pydantic validation

## Project Structure

```text
app/
  api/
    dependencies.py
    routers/
      auth.py
      bookings.py
      health.py
  core/
    config.py
    database.py
    exceptions.py
    security.py
  models/
    user.py
    booking.py
  repositories/
    auth_repository.py
    booking_repository.py
  services/
    auth_service.py
    booking_service.py
  schemas/
    auth.py
    booking.py
  main.py
```

## Prerequisites

- Python 3.11+
- MySQL access to database `lead_management`
- SSL cert + key files for HTTPS

## Setup (Without Docker)

1. Create and activate virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Copy env template:

```powershell
Copy-Item .env.example .env
```

4. Edit `.env` values (especially MySQL and JWT).

5. Put SSL files in `certs/`:

- `certs/server.crt`
- `certs/server.key`

(or update `SSL_CERT_FILE` and `SSL_KEY_FILE` in `.env`)

## Run HTTPS API on Port 2010

```powershell
py apk.py
```

Server URL:

- `https://localhost:2010`

## API Endpoints

1. `POST /api/v1/auth/login`
- Body: `username`, `password`
- Password validation uses user DOB from `users.dob`.
- Accepted formats: `DDMMYYYY`, `YYYYMMDD`, `DD-MM-YYYY`, `YYYY-MM-DD`.
- JWT expiry: 12 hours (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES=720`)

2. `GET /api/v1/bookings/my-assigned`
- Auth required (Bearer token)
- Returns active bookings only (`booking_status` in `1,2`) for logged-in assigned user

3. `GET /api/v1/bookings/my-assigned/history`
- Auth required (Bearer token)
- Returns history bookings only (`booking_status` in `3,4,5`) for logged-in assigned user
- Read-only listing for cancelled/completed records

4. `GET /api/v1/bookings/my-assigned/{booking_id}`
- Auth required (Bearer token)
- Returns booking + caller + address + patients + tests per patient
- Includes `booking_status` + per patient `booking_patient_id` and `booking_patient_status`
- Tests only from `hhome_collection_booking_patient_test` (`booked_code`, `test_name`)

5. `POST /api/v1/bookings/my-assigned/{booking_id}/status`
- Auth required (Bearer token)
- Body action: `assign` / `start` / `complete` / `completed` / `cancel`
- Uses booking + patient status workflow and returns final booking status + all patient statuses
- `complete` computes final booking as `3` (Completed) or `5` (Partial)

6. `POST /api/v1/bookings/my-assigned/{booking_id}/patients`
- Auth required (Bearer token)
- Adds a new patient into the same existing booking (same selected address linkage)
- Allowed only when booking status is `1` or `2`
- No new address is created and booking address is not changed
- Also upserts caller-mobile mapping in `hcaller_mobile_map` for patient primary/alternate mobile
- If mobile is already active-mapped to another caller, API returns `409`

7. `POST /api/v1/bookings/my-assigned/{booking_id}/patients/{booking_patient_id}/cancel`
- Auth required (Bearer token)
- Cancels only selected patient row (`booking_patient_status -> 4`)
- Booking status is not auto-finalized by this API

8. `PUT /api/v1/bookings/my-assigned/{booking_id}/patients/{patient_id}`
- Auth required (Bearer token)
- Edits existing patient details in `hpatient_master` for same booking
- If mobile changes, `hcaller_mobile_map` is synced
- If new mobile is mapped to another caller, API returns `409`

9. `GET /health`
- Health check

10. `GET /api/hc/panel-companies-lite?q=<min2>&limit=<1..50>&atype=<optional C|D>`
- Lightweight panel company search for APK flow
- Source DB: `bhasin_7001` (live fetch, no server-side preload cache)

11. `GET /api/hc/panel-tests-lite?comp_cat_id=<id>&q=<min2>&limit=<1..100>`
- Lightweight test search by panel/company
- Returns only required pricing + booking fields

12. `GET /api/hc/panel-child-tests-lite?parent_gcode=...&parent_scode=...&parent_test_code=...`
- Returns child tests for selected parent/profile test

13. `GET /api/hc/test-specimen-catalog-lite`
- Returns compact specimen catalog by `testcode1`

### Add Patient Curl
```cmd
curl.exe -X POST "https://labmate.bhasinpathlabs.com:2010/api/v1/bookings/my-assigned/11/patients" -H "Content-Type: application/json" -H "Authorization: Bearer <ACCESS_TOKEN>" -d "{\"title\":\"Mr\",\"full_name\":\"Test Patient\",\"gender\":\"Male\",\"date_of_birth\":\"2000-01-01\",\"age_years\":25,\"primary_mobile\":\"9898989898\",\"alternate_mobile\":\"9797979797\",\"email\":\"a@b.com\",\"labmate_pid\":\"1000000\",\"panel_company\":\"CGHS\",\"tag\":\"VIP\"}"
```

### Edit Patient Curl
```cmd
curl.exe -X PUT "https://labmate.bhasinpathlabs.com:2010/api/v1/bookings/my-assigned/3/patients/5" -H "Content-Type: application/json" -H "Authorization: Bearer <ACCESS_TOKEN>" -d "{\"full_name\":\"Updated Patient\",\"primary_mobile\":\"9999999999\",\"alternate_mobile\":\"8888888888\",\"panel_company\":\"CGHS\",\"tag\":\"VIP\"}"
```

## Auth Header

```http
Authorization: Bearer <token>
```

## Notes

- If `MYSQL_DB` is not `lead_management`, app startup fails by design.
- Panel/test lite APIs use separate DB `CATALOG_MYSQL_DB` (default `bhasin_7001`).
- No child/tube enrichment is performed.
- New login invalidates any older token for the same user.
- Special note: Current setup is for testing with direct HTTPS app run on port `2010` (`py apk.py`).
- Special note: For production/live deployment, keep Nginx as a separate reverse-proxy layer (domain/SSL at Nginx, app on internal port).
