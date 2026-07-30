"""Load .env from the backend project root into os.environ."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_LOADED = False
_ENV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252")


def _load_dotenv_file(path: Path, *, override: bool) -> None:
    from dotenv import load_dotenv

    last_error: Exception | None = None
    for encoding in _ENV_ENCODINGS:
        try:
            load_dotenv(path, override=override, encoding=encoding)
            return
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error


def _dotenv_values_file(path: Path) -> dict:
    from dotenv import dotenv_values

    last_error: Exception | None = None
    for encoding in _ENV_ENCODINGS:
        try:
            return dotenv_values(path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return {}


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    try:
        from dotenv import load_dotenv  # noqa: F401
    except ImportError:
        _ENV_LOADED = True
        return

    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        _load_dotenv_file(env_file, override=False)

    # Migration fallback: use main/.env until backend/.env is synced
    legacy_main_env = PROJECT_ROOT.parent / "main" / ".env"
    if legacy_main_env.is_file() and not env_file.is_file():
        _load_dotenv_file(legacy_main_env, override=False)

    whatsapp_keys = (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "WHATSAPP_GRAPH_API_VERSION",
        "WHATSAPP_TEMPLATE_NAME",
        "WHATSAPP_TEMPLATE_LANGUAGE_CODE",
        "WHATSAPP_SCAN_TEMPLATE_NAME",
        "WHATSAPP_CARD_RECEIVED_TEMPLATE_NAME",
        "WHATSAPP_BUSINESS_CARD_TEMPLATE_NAME",
        "WHATSAPP_TEMPLATE_EVENT_NAME",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_BUSINESS_PHONE",
    )
    if env_file.is_file():
        try:
            root_values = _dotenv_values_file(env_file)
            for key in whatsapp_keys:
                if os.environ.get(key):
                    continue
                value = root_values.get(key)
                if value:
                    os.environ[key] = value.strip().strip('"').strip("'")
        except Exception:
            _load_dotenv_file(env_file, override=True)

    _ENV_LOADED = True
