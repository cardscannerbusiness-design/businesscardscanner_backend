"""AWS Textract service for business card OCR (online mode).

Uses boto3 with credentials from environment variables (no hardcoded secrets).
Calls DetectDocumentText only — AnalyzeDocument FORMS was removed because it
added a full second AWS round-trip without contributing any text to parsing.
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

# Bound network waits so extract_text cannot block a worker forever.
_TEXTRACT_CONNECT_TIMEOUT = int(os.getenv("TEXTRACT_CONNECT_TIMEOUT_SEC", "10"))
_TEXTRACT_READ_TIMEOUT = int(os.getenv("TEXTRACT_READ_TIMEOUT_SEC", "60"))

_textract_client = None


def _get_textract_client():
    """Lazy-init the boto3 Textract client from env credentials."""
    global _textract_client
    if _textract_client is None:
        if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
            raise RuntimeError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY in .env."
            )
        import boto3
        from botocore.config import Config

        _textract_client = boto3.client(
            "textract",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=Config(
                connect_timeout=_TEXTRACT_CONNECT_TIMEOUT,
                read_timeout=_TEXTRACT_READ_TIMEOUT,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _textract_client


def is_textract_configured() -> bool:
    """Return True when AWS env vars are present."""
    return bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_REGION)


def extract_text(image_bytes: bytes) -> str:
    """Extract raw text from a card image via Textract DetectDocumentText.

    Returns newline-joined LINE blocks. Raises RuntimeError on auth,
    rate-limit, or network failures with typed error messages.
    """
    client = _get_textract_client()
    started = time.perf_counter()
    logger.info("[OCR] Textract DetectDocumentText Started bytes=%s", len(image_bytes or b""))

    try:
        response = client.detect_document_text(
            Document={"Bytes": image_bytes}
        )
    except Exception as exc:
        logger.exception(
            "[OCR] Textract DetectDocumentText Failed duration_ms=%.1f",
            (time.perf_counter() - started) * 1000,
        )
        _handle_textract_error(exc)
        # _handle_textract_error always raises, but keep linter happy
        raise

    blocks = response.get("Blocks", [])
    lines = [
        block.get("Text", "")
        for block in blocks
        if block.get("BlockType") == "LINE"
    ]
    raw_text = "\n".join(lines)
    logger.info(
        "[OCR] Textract DetectDocumentText Completed duration_ms=%.1f lines=%d chars=%d",
        (time.perf_counter() - started) * 1000,
        len(lines),
        len(raw_text),
    )

    return raw_text


def _handle_textract_error(exc: Exception) -> None:
    """Map boto3 ClientError codes to typed RuntimeErrors."""
    error_code = ""
    if hasattr(exc, "response"):
        error_code = exc.response.get("Error", {}).get("Code", "")

    if error_code in ("UnauthorizedOperation", "AccessDenied", "InvalidSignatureException"):
        logger.error("Textract auth error: %s", exc)
        raise RuntimeError(
            "AWS Textract authorization failed. Check IAM permissions for "
            "textract:DetectDocumentText."
        ) from exc

    if error_code in ("ProvisionedThroughputExceededException", "ThrottlingException") or "RateLimit" in error_code:
        logger.warning("Textract rate limited: %s", exc)
        raise RuntimeError(
            "AWS Textract rate limit exceeded. Please retry in a moment."
        ) from exc

    if error_code in ("InvalidParameterException", "InvalidS3ObjectException"):
        logger.error("Textract invalid input: %s", exc)
        raise RuntimeError(f"AWS Textract invalid input: {exc}") from exc

    # Network / unknown (includes ReadTimeoutError / ConnectTimeoutError)
    logger.error("Textract unexpected error: %s", exc)
    raise RuntimeError(f"Unexpected Textract error: {exc}") from exc
