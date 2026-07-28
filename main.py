import os
import re
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "leads.db"
EVENTS_LOG_PATH = BASE_DIR / "events.log"
APP_LOG_PATH = BASE_DIR / "app.log"

logger = logging.getLogger("lead_intake")
logger.setLevel(logging.INFO)
logger.handlers.clear()
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_handler = logging.FileHandler(APP_LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)
logger.propagate = False


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db() -> None:
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    source TEXT,
                    comment TEXT
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);"
            )
        logger.info("База данных инициализирована: %s", DB_PATH)
    except sqlite3.Error as exc:
        logger.exception("Не удалось инициализировать базу данных: %s", exc)
        raise


def record_event(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"{timestamp} {message}"
    try:
        with EVENTS_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line + os.linesep)
    except OSError as exc:
        logger.exception("Не удалось записать событие в events.log: %s", exc)
    logger.info(message)


def describe_validation_error(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Некорректный запрос: не удалось разобрать тело запроса."

    first = errors[0]
    loc = first.get("loc", ())
    field = ".".join(str(part) for part in loc[1:]) if len(loc) > 1 else (loc[0] if loc else "тело запроса")
    error_type = first.get("type", "")
    raw_msg = first.get("msg", "")

    if error_type == "missing":
        return f"Не передано обязательное поле «{field}»."
    if error_type == "json_invalid":
        return "Некорректный JSON в теле запроса."
    if "contact" in field and error_type == "value_error":
        return "Поле «contact» заполнено некорректно. Укажите телефон или email."
    if error_type == "string_too_short":
        return f"Поле «{field}» слишком короткое."
    if error_type == "string_too_long":
        return f"Поле «{field}» слишком длинное."
    if raw_msg:
        return f"Поле «{field}»: {raw_msg}"
    return f"Некорректное значение поля «{field}»."


app = FastAPI(title="Lead Intake Service", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    message = describe_validation_error(exc)
    logger.warning("Ошибка валидации запроса %s %s: %s", request.method, request.url.path, message)
    return JSONResponse(
        status_code=400,
        content={"status": "error", "detail": message},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": "Внутренняя ошибка сервера. Попробуйте позже."},
    )


class LeadIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    contact: str = Field(..., min_length=3, max_length=120)
    source: Optional[str] = Field(default=None, max_length=60)
    comment: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("name", "contact", "source", "comment", mode="before")
    @classmethod
    def strip_values(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("contact")
    @classmethod
    def validate_contact(cls, value: str) -> str:
        phone_pattern = re.compile(r"^\+?[0-9\-\s\(\)]{6,20}$")
        email_pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        if phone_pattern.match(value) or email_pattern.match(value):
            return value
        raise ValueError("contact must be a valid phone number or email")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/lead")
async def create_lead(payload: LeadIn) -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO leads (created_at, name, contact, source, comment)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    payload.name,
                    payload.contact,
                    payload.source,
                    payload.comment,
                ),
            )
            lead_id = cursor.lastrowid
    except sqlite3.Error as exc:
        logger.exception("Не удалось сохранить заявку в базу данных: %s", exc)
        record_event(f"ERROR: failed to save lead ({exc})")
        raise HTTPException(
            status_code=500,
            detail="Не удалось сохранить заявку: база данных недоступна. Попробуйте позже.",
        )
    except Exception as exc:
        logger.exception("Неожиданная ошибка при сохранении заявки: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка сервера. Попробуйте позже.",
        )

    record_event(f"New lead saved: {lead_id}")
    logger.info("Заявка от %s успешно принята (id=%s).", payload.name, lead_id)
    return {
        "status": "ok",
        "id": lead_id,
        "created_at": created_at,
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), reload=False)
