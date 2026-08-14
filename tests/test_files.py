from __future__ import annotations

import asyncio
import threading
import time
from io import BytesIO
from pathlib import Path

import fitz
import pytest
from PIL import Image
from starlette.datastructures import Headers, UploadFile

import app.files as files_module
from app.errors import FileRejected
from app.files import (
    StoredDocument,
    disambiguate_display_names,
    extract_document,
    save_and_validate,
    split_text_without_loss,
)


def upload(filename: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def test_text_chunking_never_drops_or_duplicates_characters():
    text = ("dòng dữ liệu 1\n" * 1_000) + ("X" * 5_000)
    chunks = split_text_without_loss(text, 2_000)

    assert len(chunks) > 1
    assert "".join(chunks) == text


def test_extracts_every_pdf_page(tmp_path):
    pdf = fitz.open()
    for page_number in range(1, 32):
        page = pdf.new_page()
        page.insert_text(
            (72, 72),
            f"PAGE-{page_number} " + ("nội dung chứng từ " * 10),
        )
    path = tmp_path / "all-pages.pdf"
    pdf.save(path)
    pdf.close()

    document = StoredDocument(
        original_name="all-pages.pdf",
        path=path,
        extension=".pdf",
        declared_mime="application/pdf",
        detected_mime="application/pdf",
    )
    parts = list(
        extract_document(document, chunk_chars=2_000, ocr_lang="vie+eng")
    )

    assert any(part.location.startswith("trang 31") for part in parts)
    assert any("PAGE-31" in part.text for part in parts)


def test_image_uses_ocr_without_real_tesseract(tmp_path, monkeypatch):
    path = tmp_path / "scan.png"
    Image.new("RGB", (40, 40), "white").save(path)
    monkeypatch.setattr(
        files_module.pytesseract,
        "image_to_string",
        lambda image, lang: "HÓA ĐƠN OCR",
    )
    document = StoredDocument(
        original_name="scan.png",
        path=path,
        extension=".png",
        declared_mime="image/png",
        detected_mime="image/png",
    )

    parts = list(
        extract_document(document, chunk_chars=2_000, ocr_lang="vie+eng")
    )

    assert parts[0].text == "HÓA ĐƠN OCR"


def test_pdf_preserves_embedded_text_when_ocr_is_added(tmp_path, monkeypatch):
    path = tmp_path / "hybrid.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "PO-EMBEDDED-123")
    pdf.save(path)
    pdf.close()
    monkeypatch.setattr(
        files_module.pytesseract,
        "image_to_string",
        lambda image, lang: "OCR-INVOICE-456",
    )
    document = StoredDocument(
        original_name="hybrid.pdf",
        path=path,
        extension=".pdf",
        declared_mime="application/pdf",
        detected_mime="application/pdf",
    )

    text = "".join(
        part.text
        for part in extract_document(
            document,
            chunk_chars=2_000,
            ocr_lang="vie+eng",
        )
    )

    assert "PO-EMBEDDED-123" in text
    assert "OCR-INVOICE-456" in text


@pytest.mark.asyncio
async def test_rejects_declared_mime_mismatch(tmp_path):
    candidate = upload("fake.pdf", b"not a pdf", "text/plain")
    with pytest.raises(FileRejected) as error:
        await save_and_validate(candidate, tmp_path)
    await candidate.close()

    assert error.value.code == "declared_mime_mismatch"


@pytest.mark.asyncio
async def test_rejects_signature_mismatch_before_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "application/pdf")
    candidate = upload("fake.pdf", b"not a pdf", "application/pdf")
    with pytest.raises(FileRejected) as error:
        await save_and_validate(candidate, tmp_path)
    await candidate.close()

    assert error.value.code == "signature_mismatch"


@pytest.mark.asyncio
async def test_rejects_xml_external_entity(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "application/xml")
    content = b'''<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<root>&xxe;</root>'''
    candidate = upload("unsafe.xml", content, "application/xml")

    with pytest.raises(FileRejected) as error:
        await save_and_validate(candidate, tmp_path)
    await candidate.close()

    assert error.value.code == "invalid_file_content"


@pytest.mark.asyncio
async def test_xml_validation_uses_streaming_iterparse(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "application/xml")
    monkeypatch.setattr(
        files_module.SafeElementTree,
        "parse",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full DOM parse must not be used")
        ),
    )
    content = (
        "<?xml version='1.0' encoding='utf-8'?><root>"
        + "".join(f"<line>{index}</line>" for index in range(5_000))
        + "</root>"
    ).encode()
    candidate = upload("large.xml", content, "application/xml")

    stored = await save_and_validate(candidate, tmp_path)
    await candidate.close()

    assert stored.path.exists()


@pytest.mark.asyncio
async def test_blocking_validation_runs_off_event_loop(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocking_validation(path, extension):
        del path, extension
        entered.set()
        assert release.wait(timeout=2)
        return "text/plain"

    monkeypatch.setattr(
        files_module,
        "_validate_saved_document",
        blocking_validation,
    )
    candidate = upload("document.txt", b"plain text", "text/plain")
    timer = threading.Timer(1.0, release.set)
    timer.start()
    started = time.monotonic()
    task = asyncio.create_task(save_and_validate(candidate, tmp_path))
    try:
        while not entered.is_set() and time.monotonic() - started < 0.4:
            await asyncio.sleep(0.01)
        assert entered.is_set()
        # If validation ran on the event loop, the one-second timer would have
        # fired before this coroutine could resume.
        assert time.monotonic() - started < 0.4
    finally:
        release.set()
        await task
        await candidate.close()
        timer.cancel()


@pytest.mark.asyncio
async def test_no_previous_25mb_application_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "detect_mime", lambda path: "text/plain")
    content = b"A" * (25 * 1024 * 1024 + 1)
    candidate = upload("large.txt", content, "text/plain")

    stored = await save_and_validate(candidate, tmp_path)
    await candidate.close()

    assert stored.path.stat().st_size == len(content)


def test_duplicate_filenames_are_disambiguated(tmp_path):
    source = StoredDocument(
        original_name="invoice.pdf",
        path=tmp_path / "one.pdf",
        extension=".pdf",
        declared_mime="application/pdf",
        detected_mime="application/pdf",
    )

    renamed = disambiguate_display_names([source, source, source])

    assert [item.original_name for item in renamed] == [
        "invoice.pdf",
        "invoice (2).pdf",
        "invoice (3).pdf",
    ]
