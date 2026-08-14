from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import anyio
import magic
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect

from .config import Settings
from .errors import (
    ApiError,
    ExtractionFailed,
    FileRejected,
    MaasUnavailable,
    StructuredOutputFailed,
)
from .files import (
    SUPPORTED_EXTENSIONS,
    disambiguate_display_names,
    save_and_validate,
)
from .maas import OpenAICompatibleGateway
from .schemas import CheckInput, ReviewResult
from .service import ReviewPipeline
from .tempwork import prepare_temp_root, request_workdir


logger = logging.getLogger(__name__)
ReadinessProbe = Callable[[], list[str]]
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # The OpenAI SDK logs request options (including full prompts) at DEBUG.
    # Clamp third-party transports regardless of the application's LOG_LEVEL.
    for dependency_logger in ("openai", "httpx", "httpcore"):
        logging.getLogger(dependency_logger).setLevel(logging.WARNING)


def _parse_bool(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    raise ApiError(422, "invalid_metadata", "Thông tin bộ chứng từ không hợp lệ.")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str | None = None,
) -> JSONResponse:
    error: dict[str, str] = {"code": code, "message": message}
    if request_id:
        error["requestId"] = request_id
    return JSONResponse(status_code=status_code, content={"error": error})


def _default_readiness_probe(settings: Settings, pipeline: ReviewPipeline | None) -> list[str]:
    issues: list[str] = []
    if not settings.maas_configured or pipeline is None:
        issues.append("maas_not_configured")
    if shutil.which("tesseract") is None:
        issues.append("ocr_not_available")
    try:
        magic.from_buffer(b"plain text", mime=True)
    except Exception:
        issues.append("mime_detector_not_available")
    try:
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.temp_dir / f".ready-{uuid4().hex}"
        probe.touch(exist_ok=False)
        probe.unlink(missing_ok=True)
    except OSError:
        issues.append("temp_dir_not_writable")
    return issues


async def _wait_for_client_disconnect(
    request: Request,
    stop: asyncio.Event,
    *,
    poll_seconds: float = 0.1,
) -> bool:
    while not stop.is_set():
        if await request.is_disconnected():
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass
    return False


async def _review_until_disconnect(
    request: Request,
    pipeline: ReviewPipeline,
    metadata: CheckInput,
    stored_documents,
) -> ReviewResult:
    """Cancel and drain review work if the client goes away.

    asyncio tasks are managed explicitly instead of TaskGroup so a normal
    disconnect cannot be wrapped in an ExceptionGroup.
    """

    watcher_stop = asyncio.Event()
    review_task = asyncio.create_task(
        pipeline.review(metadata, stored_documents),
        name="po-pr-review",
    )
    disconnect_task = asyncio.create_task(
        _wait_for_client_disconnect(request, watcher_stop),
        name="po-pr-disconnect-watcher",
    )
    try:
        done, _ = await asyncio.wait(
            {review_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if review_task in done:
            watcher_stop.set()
            return review_task.result()

        if disconnect_task.result():
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)
            raise ClientDisconnect()

        return await review_task
    finally:
        # Request.is_disconnected() uses an internally cancelled AnyIO scope.
        # Cancelling that coroutine at just the wrong point can be swallowed by
        # the inner scope, leaving a watcher loop alive. Signal a graceful stop
        # first; only review work needs forceful cancellation on outer abort.
        watcher_stop.set()
        if not review_task.done():
            review_task.cancel()
        await asyncio.gather(
            review_task,
            disconnect_task,
            return_exceptions=True,
        )


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: ReviewPipeline | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    pipeline_override = pipeline
    _configure_logging(runtime_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await anyio.to_thread.run_sync(
            prepare_temp_root,
            runtime_settings.temp_dir,
            runtime_settings.stale_temp_seconds,
        )

        active_pipeline = pipeline_override
        if active_pipeline is None and runtime_settings.maas_configured:
            active_pipeline = ReviewPipeline(
                OpenAICompatibleGateway(runtime_settings),
                runtime_settings,
            )
        application.state.pipeline = active_pipeline
        try:
            yield
        finally:
            if active_pipeline is not None:
                await active_pipeline.close()

    application = FastAPI(
        title="PO/PR Reviewing Hackathon API",
        version=runtime_settings.app_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.state.pipeline = pipeline_override

    @application.middleware("http")
    async def safe_request_logging(request: Request, call_next):
        upstream_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            upstream_request_id
            if SAFE_REQUEST_ID.fullmatch(upstream_request_id)
            else uuid4().hex
        )
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Deliberately omit exception text/traceback: parser and model
            # exceptions may contain file paths or document text.
            logger.error(
                "event=unhandled_request_error request_id=%s kind=%s",
                request_id,
                type(exc).__name__,
            )
            response = _error_response(
                500,
                "internal_error",
                "Máy chủ gặp lỗi khi xử lý yêu cầu.",
                request_id,
            )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "event=request_completed request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @application.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error_response(
            exc.status_code,
            exc.code,
            exc.public_message,
            getattr(request.state, "request_id", None),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        del exc
        return _error_response(
            422,
            "invalid_request",
            "Yêu cầu không đúng cấu trúc.",
            getattr(request.state, "request_id", None),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        public_messages = {
            404: "Không tìm thấy tài nguyên.",
            405: "Phương thức không được hỗ trợ.",
            413: "Hạ tầng phía trước đã từ chối kích thước yêu cầu.",
        }
        return _error_response(
            exc.status_code,
            f"http_{exc.status_code}",
            public_messages.get(exc.status_code, "Yêu cầu không thể được xử lý."),
            getattr(request.state, "request_id", None),
        )

    @application.get("/api/live")
    async def live():
        return {"status": "live", "version": runtime_settings.app_version}

    async def ready_response():
        active_pipeline = application.state.pipeline
        issues = (
            readiness_probe()
            if readiness_probe is not None
            else _default_readiness_probe(runtime_settings, active_pipeline)
        )
        if issues:
            # Return stable machine-readable reason codes, never config values.
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reasons": issues},
            )
        return {"status": "ready", "version": runtime_settings.app_version}

    application.add_api_route("/api/ready", ready_response, methods=["GET"])
    application.add_api_route("/api/health", ready_response, methods=["GET"])

    @application.get("/api/config")
    async def public_config():
        return {
            "appVersion": runtime_settings.app_version,
            "supportedExtensions": [item.removeprefix(".") for item in SUPPORTED_EXTENSIONS],
            "fileCountLimit": None,
            "fileSizeLimit": None,
            "pdfPageLimit": None,
            "ocrEnabled": True,
        }

    @application.post("/api/check", response_model=ReviewResult)
    async def check(request: Request):
        active_pipeline: ReviewPipeline | None = application.state.pipeline
        if active_pipeline is None:
            raise ApiError(
                503,
                "maas_not_configured",
                "Dịch vụ AI chưa được cấu hình.",
            )

        try:
            # Parse manually to override Starlette's default file-count and
            # part-size ceilings. sys.maxsize is the parser's practical no-cap
            # value; this application performs no count/byte/page rejection.
            form_context = request.form(
                max_files=sys.maxsize,
                max_fields=sys.maxsize,
                max_part_size=sys.maxsize,
            )
            async with form_context as form:
                try:
                    metadata = CheckInput(
                        eform=str(form.get("eform") or ""),
                        type=str(form.get("type") or ""),
                        is_inv=_parse_bool(form.get("isInv")),
                        item_number=str(form.get("itemNumber") or ""),
                        payee=str(form.get("payee") or "NCC"),
                    )
                except ValidationError as exc:
                    raise ApiError(
                        422,
                        "invalid_metadata",
                        "Thông tin bộ chứng từ không hợp lệ.",
                    ) from exc

                file_items = [*form.getlist("files"), *form.getlist("files[]")]
                if not file_items or not all(
                    isinstance(item, UploadFile) for item in file_items
                ):
                    raise ApiError(
                        422,
                        "files_required",
                        "Cần chọn ít nhất một file chứng từ.",
                    )

                with request_workdir(runtime_settings.temp_dir) as workdir:
                    stored_documents = []
                    try:
                        for item in file_items:
                            assert isinstance(item, UploadFile)
                            stored_documents.append(
                                await save_and_validate(item, workdir)
                            )
                        stored_documents = disambiguate_display_names(
                            stored_documents
                        )
                        return await _review_until_disconnect(
                            request,
                            active_pipeline,
                            metadata,
                            stored_documents,
                        )
                    except FileRejected as exc:
                        status = 507 if exc.code == "temporary_write_failed" else 415
                        raise ApiError(
                            status,
                            exc.code,
                            exc.public_message,
                        ) from exc
                    except ExtractionFailed as exc:
                        raise ApiError(
                            422,
                            "extraction_failed",
                            "Không thể đọc đầy đủ nội dung chứng từ.",
                        ) from exc
                    except MaasUnavailable as exc:
                        raise ApiError(
                            502,
                            "maas_unavailable",
                            "Dịch vụ AI tạm thời không phản hồi.",
                        ) from exc
                    except StructuredOutputFailed as exc:
                        raise ApiError(
                            502,
                            "invalid_ai_result",
                            "AI chưa trả về kết quả đúng cấu trúc.",
                        ) from exc

        except (MultiPartException, ClientDisconnect) as exc:
            raise ApiError(
                400,
                "invalid_multipart",
                "Dữ liệu upload không hoàn chỉnh hoặc không đúng multipart.",
            ) from exc

    # API routes are registered first. Static frontend is therefore served
    # from the exact same origin without any permissive CORS middleware.
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    application.mount(
        "/",
        StaticFiles(directory=str(frontend_dir), html=True, check_dir=False),
        name="frontend",
    )
    return application


app = create_app()
