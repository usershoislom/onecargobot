"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _csv(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_id: int | None
    spreadsheet_id: str
    google_credentials_file: str
    gemini_api_key: str
    gemini_models: list[str]
    groq_api_key: str | None
    groq_text_model: str
    groq_whisper_model: str
    timezone: ZoneInfo
    batch_window_seconds: float
    recent_rows_context_limit: int = 300
    fallback_doc_chars: int = 9000
    ocr_max_pages: int = 20
    log_level: str = "INFO"


def load_config() -> Config:
    _load_dotenv(ROOT / ".env")

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise SystemExit(f"Missing required environment variable: {name} (see .env.example)")
        return value

    allowed = os.environ.get("ALLOWED_USER_ID", "").strip()
    return Config(
        telegram_token=required("TELEGRAM_BOT_TOKEN"),
        allowed_user_id=int(allowed) if allowed else None,
        spreadsheet_id=required("SPREADSHEET_ID"),
        google_credentials_file=os.environ.get("GOOGLE_CREDENTIALS_FILE", str(ROOT / "service_account.json")),
        gemini_api_key=required("GEMINI_API_KEY"),
        gemini_models=_csv("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.1-flash-lite"),
        groq_api_key=os.environ.get("GROQ_API_KEY", "").strip() or None,
        groq_text_model=os.environ.get("GROQ_TEXT_MODEL", "openai/gpt-oss-120b"),
        groq_whisper_model=os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
        timezone=ZoneInfo(os.environ.get("TIMEZONE", "America/New_York")),
        batch_window_seconds=float(os.environ.get("BATCH_WINDOW_SECONDS", "4")),
        recent_rows_context_limit=int(os.environ.get("RECENT_ROWS_CONTEXT_LIMIT", "300")),
        fallback_doc_chars=int(os.environ.get("FALLBACK_DOC_CHARS", "9000")),
        ocr_max_pages=int(os.environ.get("OCR_MAX_PAGES", "20")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
