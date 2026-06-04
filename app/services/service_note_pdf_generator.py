from __future__ import annotations

import logging
import os
import base64
import json
import shutil
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.config import get_settings
from app.core.database import CatalogSessionLocal, SessionLocal
from sqlalchemy import bindparam, text
from tools.service_note_html import build_service_note_html


logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="service-note-pdf")
_active_jobs: set[str] = set()
_active_jobs_lock = threading.Lock()

_WINDOWS_BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
_LINUX_BROWSER_COMMANDS = (
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)

_settings = get_settings()


def _job_key(booking_id: int | str) -> str:
    key = str(booking_id).strip()
    if not key:
        raise ValueError("booking_id is required")
    return key


def _generated_root() -> Path:
    return Path(__file__).resolve().parents[1] / "static" / "generated" / "service_notes"


def _normalize_indian_whatsapp_number(value: object) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("91") and len(digits) == 12:
        return digits
    if len(digits) == 10:
        return f"91{digits}"
    return None


def _normalize_local_whatsapp_target(value: object) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) >= 16:
        return f"{digits}@g.us"
    return _normalize_indian_whatsapp_number(digits)


def _pdf_public_url(booking_key: str) -> str:
    base = str(_settings.apk_public_domain or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("APK public domain is missing for service note PDF URL generation")
    return f"{base}/static/generated/service_notes/{booking_key}/service_note_{booking_key}.pdf"


def _patient_whatsapp_targets(payload: dict) -> list[dict[str, str]]:
    grouped: dict[str, list[str]] = {}
    seen_names_by_mobile: dict[str, set[str]] = {}
    for row in (payload.get("sample_collection_pick_patients") or []):
        if not isinstance(row, dict):
            continue
        try:
            booking_patient_status = int(row.get("booking_patient_status") or 0)
        except (TypeError, ValueError):
            booking_patient_status = 0
        if booking_patient_status != 3:
            continue
        patient_name = str(row.get("name") or row.get("full_name") or "").strip()
        mobile = _normalize_indian_whatsapp_number(row.get("contact_mobile"))
        if not patient_name or not mobile:
            continue
        seen_names = seen_names_by_mobile.setdefault(mobile, set())
        if patient_name in seen_names:
            continue
        seen_names.add(patient_name)
        grouped.setdefault(mobile, []).append(patient_name)
    return [
        {"patient_name": ", ".join(names), "recipient_whatsapp": mobile}
        for mobile, names in grouped.items()
        if names
    ]


def _completed_patient_names(payload: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in (payload.get("sample_collection_pick_patients") or []):
        if not isinstance(row, dict):
            continue
        try:
            booking_patient_status = int(row.get("booking_patient_status") or 0)
        except (TypeError, ValueError):
            booking_patient_status = 0
        if booking_patient_status != 3:
            continue
        name = str(row.get("name") or row.get("full_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _panel_company_names(payload: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in (payload.get("sample_collection_pick_patients") or []):
        if not isinstance(row, dict):
            continue
        try:
            booking_patient_status = int(row.get("booking_patient_status") or 0)
        except (TypeError, ValueError):
            booking_patient_status = 0
        if booking_patient_status != 3:
            continue
        name = str(row.get("panel_company") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _patient_ref_by_names(payload: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in (payload.get("patient_updates") or []):
        if not isinstance(row, dict):
            continue
        try:
            booking_patient_status = int(row.get("booking_patient_status") or 0)
        except (TypeError, ValueError):
            booking_patient_status = 0
        if booking_patient_status != 3:
            continue
        name = str(row.get("ref_by") or row.get("referred_by") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _service_note_local_message(payload: dict) -> str:
    patient_names = ", ".join(_completed_patient_names(payload)) or "Patient"
    return (
        f"Hello {patient_names},\n"
        "Your home sample collection has been completed.\n"
        "We hope everything went smoothly and that you’re feeling comfortable.\n"
        "Your samples are being sent to our into our laboratory with utmost care, "
        "where they are carefully processed, and every report is reviewed by us.\n"
        "You’ve done your bit — as your doctors, we take it forward from here and "
        "will get back to you with your reports.\n"
        "We’ll also be reaching out to understand your experience, as it helps us care"
    )


def _lookup_panel_group_targets(payload: dict) -> list[str]:
    panel_names = _panel_company_names(payload)
    if not panel_names:
        return []
    targets: list[str] = []
    seen: set[str] = set()
    stmt = text(
        """
        SELECT DISTINCT OFax
        FROM address
        WHERE pname IN :panel_names
          AND OFax IS NOT NULL
          AND TRIM(OFax) <> ''
        """
    ).bindparams(bindparam("panel_names", expanding=True))
    db = CatalogSessionLocal()
    try:
        for row in db.execute(stmt, {"panel_names": panel_names}).mappings():
            target = _normalize_local_whatsapp_target(row.get("OFax"))
            if not target or target in seen:
                continue
            seen.add(target)
            targets.append(target)
    except Exception:
        logger.exception("[service note local whatsapp] panel OFax lookup failed panels=%s", panel_names)
    finally:
        db.close()
    return targets


def _lookup_patient_ref_by_group_targets(payload: dict) -> list[str]:
    ref_names = _patient_ref_by_names(payload)
    if not ref_names:
        return []
    targets: list[str] = []
    seen: set[str] = set()
    stmt = text(
        """
        SELECT DISTINCT OFax
        FROM address
        WHERE Active = 1
          AND UPPER(TRIM(Atype)) = 'D'
          AND OFax IS NOT NULL
          AND TRIM(OFax) <> ''
          AND (
            LOWER(TRIM(pname)) IN :ref_names
            OR LOWER(TRIM(ABARID)) IN :ref_names
          )
        """
    ).bindparams(bindparam("ref_names", expanding=True))
    db = CatalogSessionLocal()
    try:
        for row in db.execute(stmt, {"ref_names": [name.lower() for name in ref_names]}).mappings():
            target = _normalize_local_whatsapp_target(row.get("OFax"))
            if not target or target in seen:
                continue
            seen.add(target)
            targets.append(target)
    except Exception:
        logger.exception("[service note local whatsapp] patient ref_by OFax lookup failed refs=%s", ref_names)
    finally:
        db.close()
    return targets


def _lookup_internal_ref_targets(payload: dict) -> list[str]:
    internal_ref = str(payload.get("intrnl_rfrncd_by") or "").strip()
    if not internal_ref:
        return []
    db = SessionLocal()
    try:
        if internal_ref.isdigit():
            row = db.execute(
                text(
                    """
                    SELECT contact
                    FROM users
                    WHERE id = :user_id
                      AND contact IS NOT NULL
                      AND TRIM(contact) <> ''
                    LIMIT 1
                    """
                ),
                {"user_id": int(internal_ref)},
            ).mappings().first()
        else:
            row = db.execute(
                text(
                    """
                    SELECT contact
                    FROM users
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name))
                      AND contact IS NOT NULL
                      AND TRIM(contact) <> ''
                    ORDER BY id ASC
                    LIMIT 1
                    """
                ),
                {"name": internal_ref},
            ).mappings().first()
        target = _normalize_local_whatsapp_target(row.get("contact") if row else None)
        return [target] if target else []
    except Exception:
        logger.exception("[service note local whatsapp] internal ref lookup failed value=%s", internal_ref)
        return []
    finally:
        db.close()


def _local_whatsapp_targets(payload: dict) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for target in (
        _lookup_panel_group_targets(payload)
        + _lookup_patient_ref_by_group_targets(payload)
        + _lookup_internal_ref_targets(payload)
    ):
        if not target or target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets


def _send_service_note_local_whatsapp(payload: dict, booking_key: str, pdf_path: Path) -> None:
    if not _settings.local_whatsapp_enabled:
        logger.info("[service note local whatsapp] disabled booking_id=%s", booking_key)
        return
    api_url = str(_settings.local_whatsapp_api_url or "").strip()
    if not api_url:
        logger.info("[service note local whatsapp] api url missing; skipped booking_id=%s", booking_key)
        return
    if not pdf_path.exists():
        logger.info("[service note local whatsapp] pdf missing; skipped booking_id=%s pdf=%s", booking_key, pdf_path)
        return

    targets = _local_whatsapp_targets(payload)
    if not targets:
        logger.info("[service note local whatsapp] no extra recipients booking_id=%s", booking_key)
        return

    message = _service_note_local_message(payload)
    pdf_data = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    timeout = int(_settings.pepipost_wa_timeout or 8)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    for target in targets:
        request_payload = {
            "accountId": int(_settings.local_whatsapp_account_id or 1),
            "target": target,
            "message": message,
            "media": {
                "data": pdf_data,
                "mimetype": "application/pdf",
                "filename": pdf_path.name,
            },
        }
        data = json.dumps(request_payload).encode("utf-8")
        request = urllib.request.Request(
            api_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                logger.info(
                    "[service note local whatsapp] sent booking_id=%s target=%s status=%s response=%s",
                    booking_key,
                    target,
                    response.status,
                    body,
                )
        except Exception:
            logger.exception(
                "[service note local whatsapp] failed booking_id=%s target=%s pdf=%s",
                booking_key,
                target,
                pdf_path,
            )


def _send_service_note_whatsapp(payload: dict, booking_key: str, pdf_path: Path) -> None:
    if not _settings.pepipost_wa_enabled:
        logger.info("[service note whatsapp] disabled booking_id=%s", booking_key)
        return
    token = str(_settings.pepipost_wa_token or "").strip()
    if not token:
        logger.info("[service note whatsapp] token missing; skipped booking_id=%s", booking_key)
        return

    pdf_url = _pdf_public_url(booking_key)
    template_name = str(_settings.pepipost_service_note_template or "newhomec").strip() or "newhomec"
    timeout = int(_settings.pepipost_wa_timeout or 8)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    media_source = "461089f9-1000-4211-b182-c7f0291f3d45"
    media_apiheader = "custom_data"
    pdf_filename = pdf_path.name

    for target in _patient_whatsapp_targets(payload):
        request_payload = {
            "message": [
                {
                    "recipient_whatsapp": target["recipient_whatsapp"],
                    "message_type": "media_template",
                    "recipient_type": "individual",
                    "source": media_source,
                    "x-apiheader": media_apiheader,
                    "type_media_template": {
                        "type": "document",
                        "url": pdf_url,
                        "filename": pdf_filename,
                    },
                    "type_template": [
                        {
                            "name": template_name,
                            "attributes": [target["patient_name"]],
                            "language": {"locale": "en", "policy": "deterministic"},
                        }
                    ],
                }
            ]
        }
        data = json.dumps(request_payload).encode("utf-8")
        request = urllib.request.Request(
            "https://waapi.pepipost.com/api/v2/message/",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                logger.info(
                    "[service note whatsapp] sent booking_id=%s recipient=%s status=%s pdf=%s response=%s",
                    booking_key,
                    target["recipient_whatsapp"],
                    response.status,
                    pdf_path,
                    body,
                )
        except Exception:
            logger.exception(
                "[service note whatsapp] failed booking_id=%s recipient=%s pdf=%s",
                booking_key,
                target["recipient_whatsapp"],
                pdf_path,
            )


def _find_headless_browser() -> str:
    for raw_path in _WINDOWS_BROWSER_PATHS:
        path = Path(raw_path)
        if path.exists():
            return str(path)
    for command in _LINUX_BROWSER_COMMANDS:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    raise FileNotFoundError("No supported Chrome/Chromium/Edge executable found for service note PDF generation")


def print_html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    html_file = Path(html_path).resolve()
    pdf_file = Path(pdf_path).resolve()
    if not html_file.exists():
        raise FileNotFoundError(f"Service note HTML not found: {html_file}")

    pdf_file.parent.mkdir(parents=True, exist_ok=True)
    browser = _find_headless_browser()
    command = [
        browser,
        "--headless",
        "--disable-gpu",
        f"--print-to-pdf={pdf_file}",
    ]
    if os.name != "nt":
        command.append("--no-sandbox")
    command.append(html_file.as_uri())

    result = subprocess.run(
        command,
        timeout=60,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Service note PDF generation failed: "
            f"returncode={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    if not pdf_file.exists() or pdf_file.stat().st_size <= 0:
        raise RuntimeError(f"Service note PDF was not created: {pdf_file}")


def generate_service_note_pdf(payload: dict, booking_id: int | str) -> Path:
    booking_key = _job_key(booking_id)
    output_dir = _generated_root() / booking_key
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / f"service_note_{booking_key}.html"
    pdf_path = output_dir / f"service_note_{booking_key}.pdf"

    build_service_note_html(payload, html_path)
    print_html_to_pdf(html_path, pdf_path)
    return pdf_path


def _cleanup_service_note_html(booking_key: str) -> None:
    html_path = _generated_root() / booking_key / f"service_note_{booking_key}.html"
    try:
        if html_path.exists():
            html_path.unlink()
            logger.info("[service note pdf] html deleted booking_id=%s html=%s", booking_key, html_path)
    except Exception:
        logger.exception("[service note pdf] html cleanup failed booking_id=%s html=%s", booking_key, html_path)


def _run_generation_job(payload: dict, booking_key: str) -> None:
    try:
        pdf_path = generate_service_note_pdf(payload, booking_key)
        logger.info("[service note pdf] generated booking_id=%s pdf=%s", booking_key, pdf_path)
        _send_service_note_whatsapp(payload, booking_key, pdf_path)
        _send_service_note_local_whatsapp(payload, booking_key, pdf_path)
        _cleanup_service_note_html(booking_key)
    except Exception:
        logger.exception("[service note pdf] generation failed booking_id=%s", booking_key)
    finally:
        with _active_jobs_lock:
            _active_jobs.discard(booking_key)


def submit_pdf_generation(payload: dict, booking_id: int | str) -> None:
    booking_key = _job_key(booking_id)
    with _active_jobs_lock:
        if booking_key in _active_jobs:
            logger.info("[service note pdf] skipped duplicate active job booking_id=%s", booking_key)
            return
        _active_jobs.add(booking_key)

    try:
        _executor.submit(_run_generation_job, payload, booking_key)
        logger.info("[service note pdf] queued booking_id=%s", booking_key)
    except Exception:
        with _active_jobs_lock:
            _active_jobs.discard(booking_key)
        logger.exception("[service note pdf] queue submit failed booking_id=%s", booking_key)
