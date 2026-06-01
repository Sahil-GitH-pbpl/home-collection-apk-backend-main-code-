from __future__ import annotations

import logging
import os
import json
import shutil
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.config import get_settings
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


def _pdf_public_url(booking_key: str) -> str:
    base = str(_settings.apk_public_domain or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("APK public domain is missing for service note PDF URL generation")
    return f"{base}/static/generated/service_notes/{booking_key}/service_note_{booking_key}.pdf"


def _patient_whatsapp_targets(payload: dict) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
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
        key = (patient_name, mobile)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"patient_name": patient_name, "recipient_whatsapp": mobile})
    return targets


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
