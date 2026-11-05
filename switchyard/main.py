import json
import logging
import sys
import time
import uuid

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import routes_admin, routes_public
from .db import SessionLocal
from .redis_client import get_redis


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("switchyard.request")

app = FastAPI(title="Switchyard", version="0.1.0")
app.include_router(routes_admin.router)
app.include_router(routes_public.router)


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            "fields": {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            }
        },
    )
    return response


@app.get("/health", tags=["ops"])
def health():
    """Is the process up? Load balancers can restart it based on this."""
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready():
    """Can it do useful work? Redis being down degrades the API but doesn't stop it."""
    checks = {}
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except SQLAlchemyError:
        checks["database"] = "down"

    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except redis.RedisError:
        checks["redis"] = "down"

    healthy = checks["database"] == "ok"
    return JSONResponse(checks, status_code=200 if healthy else 503)
