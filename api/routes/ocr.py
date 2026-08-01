"""POST /api/ocr — online OCR via AWS Textract.

Accepts a multipart image upload, calls Textract to extract text, then
parses with the existing parse_business_card parser. Returns the
structured contact data for the frontend Review/edit screen.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from auth.dependencies import get_current_user
from services.textract_service import extract_text, is_textract_configured
from utils.parser_utils import parse_business_card

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["OCR"])

# Hard ceiling so "Extracting contact details…" never spins forever.
_OCR_TIMEOUT_SEC = 90.0


@router.post(
    "/ocr",
    summary="Extract contact data from a business card image",
    description=(
        "Upload a card image. AWS Textract extracts raw text, then the "
        "parser structures it into name, company, email, phone, website, "
        "address, etc. Use this when the device is online; offline falls "
        "back to browser-based PaddleOCR."
    ),
)
async def ocr_card(
    request: Request,
    file: UploadFile = File(..., description="Business card image (PNG/JPEG)."),
):
    req_started = time.perf_counter()
    logger.info("[OCR] Request Received")

    # Enforce authentication (raises 401 if not logged in)
    get_current_user(request)
    logger.info(
        "[OCR] Auth OK duration_ms=%.1f",
        (time.perf_counter() - req_started) * 1000,
    )

    if not is_textract_configured():
        raise HTTPException(
            status_code=503,
            detail="AWS Textract is not configured on the backend.",
        )

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload.")

    logger.info(
        "[OCR] Image Received bytes=%s duration_ms=%.1f",
        len(image_bytes),
        (time.perf_counter() - req_started) * 1000,
    )

    # Validate content type loosely
    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=415,
            detail=f"Expected an image file, got '{content_type}'.",
        )

    # Storage validation must NEVER run on the OCR path. Quota checks belong
    # to contact create/update only — keep OCR independent so scans cannot
    # hang behind SELECT … FOR UPDATE.

    ocr_started = time.perf_counter()
    logger.info("[OCR] OCR Request Started")
    try:
        # Run blocking boto3 off the event loop. A sync extract_text() inside
        # this async route previously froze FastAPI for all users whenever
        # Textract stalled (or while storage/config held the loop on OCTET_LENGTH).
        raw_text = await asyncio.wait_for(
            asyncio.to_thread(extract_text, image_bytes),
            timeout=_OCR_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError as exc:
        logger.error(
            "[OCR] OCR Request Timed Out duration_ms=%.1f timeout_sec=%s",
            (time.perf_counter() - ocr_started) * 1000,
            _OCR_TIMEOUT_SEC,
        )
        raise HTTPException(
            status_code=504,
            detail=f"OCR timed out after {_OCR_TIMEOUT_SEC:.0f}s. Please retry.",
        ) from exc
    except RuntimeError as exc:
        logger.exception("[OCR] OCR failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[OCR] Unexpected OCR failure")
        raise HTTPException(status_code=502, detail=f"OCR failed: {exc}") from exc

    logger.info(
        "[OCR] OCR Response Received duration_ms=%.1f chars=%s",
        (time.perf_counter() - ocr_started) * 1000,
        len(raw_text or ""),
    )

    contact = parse_business_card(raw_text)
    logger.info(
        "[API] Returning Success Response duration_ms=%.1f",
        (time.perf_counter() - req_started) * 1000,
    )

    return {
        "engine": "textract",
        "rawText": raw_text,
        "contact": contact,
    }
