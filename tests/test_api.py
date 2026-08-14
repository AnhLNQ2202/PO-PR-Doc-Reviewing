from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request as StarletteRequest

import app.files as files_module
from app.config import Settings
from app.errors import StructuredOutputFailed
from app.main import create_app
from app.tempwork import (
    REQUEST_PREFIX,
    TESSERACT_PREFIX,
    prepare_temp_root,
)
from conftest import make_review


class FakePipeline:
    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail
        self.documents = []
        self.paths: list[Path] = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def review(self, metadata, documents):
        assert metadata.type == "PO/PR"
        self.documents = documents
        self.paths = [document.path for document in documents]
        assert all(path.exists() for path in self.paths)
        if self.fail:
            raise self.fail
        return make_review([document.original_name for document in documents])


class HangingPipeline(FakePipeline):
    def __init__(self):
        super().__init__()
        self.started = False
        self.cancelled = False

    async def review(self, metadata, documents):
        assert metadata.type == "PO/PR"
        self.documents = documents
        self.paths = [document.path for document in documents]
        self.started = True
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


def make_app(settings, pipeline):
    return create_app(
        settings,
        pipeline=pipeline,
        readiness_probe=lambda: [],
    )


def text_files(count: int):
    return [
        ("files", (f"doc-{index}.txt", f"data {index}".encode(), "text/plain"))
        for index in range(count)
    ]


def form_data():
    return {
        "eform": "FA-PM260721005",
        "type": "PO/PR",
        "isInv": "false",
        "itemNumber": "",
        "payee": "NCC",
    }


def test_live_ready_config_and_same_origin_policy(
    settings_factory,
    monkeypatch,
):
    pipeline = FakePipeline()
    app = make_app(settings_factory(), pipeline)

    with TestClient(app) as client:
        assert client.get("/api/live").status_code == 200
        assert client.get("/api/ready").json()["status"] == "ready"
        response = client.get(
            "/api/config",
            headers={"Origin": "https://untrusted.example"},
        )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    body = response.json()
    assert body["fileCountLimit"] is None
    assert body["fileSizeLimit"] is None
    assert body["pdfPageLimit"] is None
    assert "api" not in str(body).lower()


def test_debug_level_never_enables_dependency_prompt_logging(settings_factory):
    create_app(
        settings_factory(log_level="DEBUG"),
        pipeline=FakePipeline(),
        readiness_probe=lambda: [],
    )

    for logger_name in ("openai", "httpx", "httpcore"):
        assert logging.getLogger(logger_name).getEffectiveLevel() >= logging.WARNING


def test_http_maas_requires_explicit_insecure_opt_in(tmp_path):
    common = {
        "maas_api_key": "secret",
        "maas_base_url": "http://maas.example.test/v1",
        "temp_dir": tmp_path,
    }
    with pytest.raises(ValidationError):
        Settings(**common)

    accepted = Settings(**common, allow_insecure_maas_http=True)
    assert accepted.maas_base_url.startswith("http://")


def test_accepts_more_than_previous_ten_files_and_cleans_temp(
    settings_factory,
    monkeypatch,
):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    settings = settings_factory()
    pipeline = FakePipeline()
    app = make_app(settings, pipeline)

    with TestClient(app) as client:
        response = client.post(
            "/api/check",
            data=form_data(),
            files=text_files(12),
        )

    assert response.status_code == 200, response.text
    assert len(response.json()["docs"]) == 12
    assert all(not path.exists() for path in pipeline.paths)
    assert list(settings.temp_dir.iterdir()) == []


def test_duplicate_upload_names_remain_distinct(settings_factory, monkeypatch):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    pipeline = FakePipeline()
    app = make_app(settings_factory(), pipeline)

    with TestClient(app) as client:
        response = client.post(
            "/api/check",
            data=form_data(),
            files=[
                ("files", ("invoice.txt", b"one", "text/plain")),
                ("files", ("invoice.txt", b"two", "text/plain")),
            ],
        )

    assert response.status_code == 200
    assert [item["ten_file"] for item in response.json()["docs"]] == [
        "invoice.txt",
        "invoice (2).txt",
    ]


def test_cleanup_on_ai_failure_and_sanitized_error(
    settings_factory,
    monkeypatch,
):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    settings = settings_factory()
    pipeline = FakePipeline(
        fail=StructuredOutputFailed("SECRET-DOCUMENT-CONTENT")
    )
    app = make_app(settings, pipeline)

    with TestClient(app) as client:
        response = client.post(
            "/api/check",
            data=form_data(),
            files=text_files(1),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_ai_result"
    assert "SECRET" not in response.text
    assert all(not path.exists() for path in pipeline.paths)
    assert list(settings.temp_dir.iterdir()) == []


def test_disconnect_cancels_pipeline_and_cleans_request_files(
    settings_factory,
    monkeypatch,
):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    pipeline = HangingPipeline()

    async def disconnect_after_pipeline_starts(self):
        del self
        while not pipeline.started:
            await asyncio.sleep(0)
        return True

    monkeypatch.setattr(
        StarletteRequest,
        "is_disconnected",
        disconnect_after_pipeline_starts,
    )
    settings = settings_factory()
    app = make_app(settings, pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/check",
            data=form_data(),
            files=text_files(1),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_multipart"
    assert pipeline.cancelled
    assert all(not path.exists() for path in pipeline.paths)
    assert list(settings.temp_dir.iterdir()) == []
    assert "ExceptionGroup" not in response.text


def test_unexpected_error_does_not_leak_message(
    settings_factory,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    pipeline = FakePipeline(fail=RuntimeError("SECRET-INTERNAL-DOCUMENT"))
    app = make_app(settings_factory(), pipeline)

    with caplog.at_level(logging.ERROR):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/check",
                data=form_data(),
                files=text_files(1),
            )

    assert response.status_code == 500
    assert "SECRET" not in response.text
    assert "SECRET" not in caplog.text


def test_declared_mime_error_uses_frontend_error_envelope(
    settings_factory,
):
    app = make_app(settings_factory(), FakePipeline())
    with TestClient(app) as client:
        response = client.post(
            "/api/check",
            data=form_data(),
            files={"files": ("fake.pdf", b"not-pdf", "text/plain")},
        )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "declared_mime_mismatch"
    assert isinstance(response.json()["error"]["message"], str)


def test_request_id_reuses_only_safe_upstream_value(settings_factory):
    app = make_app(settings_factory(), FakePipeline())
    with TestClient(app) as client:
        accepted = client.get(
            "/api/live",
            headers={"X-Request-ID": "nginx_12345678"},
        )
        rejected = client.get(
            "/api/live",
            headers={"X-Request-ID": "bad id with spaces"},
        )

    assert accepted.headers["X-Request-ID"] == "nginx_12345678"
    assert rejected.headers["X-Request-ID"] != "bad id with spaces"


def test_startup_cleanup_deletes_only_request_prefix(tmp_path):
    root = tmp_path / "temp-root"
    root.mkdir()
    abandoned = root / f"{REQUEST_PREFIX}abandoned"
    abandoned.mkdir()
    (abandoned / "document.pdf").write_bytes(b"sensitive")
    unrelated = root / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    tess_file = root / f"{TESSERACT_PREFIX}input.PNG"
    tess_file.write_bytes(b"temporary OCR image")
    tess_dir = root / f"{TESSERACT_PREFIX}directory"
    tess_dir.mkdir()
    (tess_dir / "result.txt").write_text("temporary", encoding="utf-8")

    outside_target = tmp_path / "outside-target.txt"
    outside_target.write_text("must survive", encoding="utf-8")
    tess_link = root / f"{TESSERACT_PREFIX}link"
    try:
        tess_link.symlink_to(outside_target)
        symlink_created = True
    except OSError:
        # Windows may not grant symlink privileges to the test process.
        symlink_created = False

    prepare_temp_root(root, stale_seconds=0)

    assert not abandoned.exists()
    assert not tess_file.exists()
    assert not tess_dir.exists()
    if symlink_created:
        assert not tess_link.exists()
        assert outside_target.read_text(encoding="utf-8") == "must survive"
    assert unrelated.read_text(encoding="utf-8") == "keep"
