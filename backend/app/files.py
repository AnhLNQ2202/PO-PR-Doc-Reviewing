from __future__ import annotations

import codecs
import csv
import re
import sys
import threading
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import anyio
import fitz
import magic
import pytesseract
from charset_normalizer import from_bytes
from defusedxml import ElementTree as SafeElementTree
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
from starlette.datastructures import UploadFile

from .errors import ExtractionFailed, FileRejected


SUPPORTED_EXTENSIONS = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".xml",
    ".txt",
    ".csv",
)

DECLARED_MIMES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg", "image/jpg"}),
    ".jpeg": frozenset({"image/jpeg", "image/jpg"}),
    ".webp": frozenset({"image/webp"}),
    ".xml": frozenset({"application/xml", "text/xml"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset(
        {"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"}
    ),
}

DETECTED_MIMES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".webp": frozenset({"image/webp"}),
    # libmagic commonly reports XML and CSV as plain text.
    ".xml": frozenset({"application/xml", "text/xml", "text/plain"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset({"text/csv", "text/plain", "application/csv"}),
}

# csv.field_size_limit is process-global. Serializing the temporary change
# prevents concurrent validations from restoring a smaller limit mid-parse.
_CSV_FIELD_LIMIT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class StoredDocument:
    original_name: str
    path: Path
    extension: str
    declared_mime: str
    detected_mime: str


@dataclass(frozen=True, slots=True)
class ExtractedPart:
    filename: str
    location: str
    text: str


def disambiguate_display_names(
    documents: list[StoredDocument],
) -> list[StoredDocument]:
    """Give duplicate browser filenames stable, visible, unique display names."""

    used: set[str] = set()
    result: list[StoredDocument] = []
    for document in documents:
        original = document.original_name
        candidate = original
        stem = Path(original).stem
        suffix = Path(original).suffix
        ordinal = 2
        while candidate.casefold() in used:
            candidate = f"{stem} ({ordinal}){suffix}"
            ordinal += 1
        used.add(candidate.casefold())
        result.append(
            document
            if candidate == original
            else replace(document, original_name=candidate)
        )
    return result


def sanitize_filename(filename: str | None) -> str:
    """Return a display-only filename; never return a user-controlled path."""

    candidate = re.split(r"[\\/]", filename or "")[-1]
    candidate = "".join(char for char in candidate if char.isprintable()).strip()
    if not candidate:
        raise FileRejected("missing_filename", "Tên file bị thiếu.")
    if len(candidate) > 500:
        raise FileRejected("filename_too_long", "Tên file quá dài.")
    return candidate


def detect_mime(path: Path) -> str:
    """Isolated for deterministic unit tests and platform-specific libmagic."""

    try:
        return str(magic.from_file(str(path), mime=True)).lower().strip()
    except Exception as exc:  # pragma: no cover - installation failure
        raise FileRejected(
            "mime_detection_failed",
            "Không xác định được loại nội dung thực tế của file.",
        ) from exc


def _validate_signature(path: Path, extension: str) -> None:
    with path.open("rb") as stream:
        head = stream.read(1_024)

    valid = True
    if extension == ".pdf":
        valid = b"%PDF-" in head
    elif extension == ".png":
        valid = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = head.startswith(b"\xff\xd8\xff")
    elif extension == ".webp":
        valid = (
            len(head) >= 12
            and head.startswith(b"RIFF")
            and head[8:12] == b"WEBP"
        )

    if not valid:
        raise FileRejected(
            "signature_mismatch",
            "Đuôi file không khớp chữ ký nội dung thực tế.",
        )


def _detect_text_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        sample = stream.read(256 * 1024)

    if not sample:
        return "utf-8"
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\x00" in sample:
        raise FileRejected(
            "binary_text_file",
            "File văn bản chứa dữ liệu nhị phân không hợp lệ.",
        )

    match = from_bytes(sample).best()
    if match is None or not match.encoding:
        raise FileRejected(
            "text_encoding_unknown",
            "Không xác định được bảng mã của file văn bản.",
        )
    return str(match.encoding)


def _validate_parser(path: Path, extension: str) -> None:
    try:
        if extension == ".pdf":
            with fitz.open(path) as document:
                if not document.is_pdf or document.needs_pass:
                    raise FileRejected(
                        "encrypted_or_invalid_pdf",
                        "PDF không hợp lệ hoặc đang được bảo vệ bằng mật khẩu.",
                    )
                # Access every page object so damage late in the PDF is detected.
                for page_number in range(document.page_count):
                    document.load_page(page_number)

        elif extension in {".png", ".jpg", ".jpeg", ".webp"}:
            with Image.open(path) as image:
                image.verify()

        elif extension == ".xml":
            # defusedxml rejects DTD/entity expansion/XXE. This is parser safety,
            # not a malware scan. iterparse + clear validates without retaining
            # a complete ElementTree for an arbitrarily large XML file.
            for _, element in SafeElementTree.iterparse(path, events=("end",)):
                element.clear()

        elif extension == ".txt":
            encoding = _detect_text_encoding(path)
            with path.open("r", encoding=encoding, errors="strict") as stream:
                while stream.read(1024 * 1024):
                    pass

        elif extension == ".csv":
            encoding = _detect_text_encoding(path)
            with _CSV_FIELD_LIMIT_LOCK:
                old_limit = csv.field_size_limit()
                try:
                    try:
                        csv.field_size_limit(sys.maxsize)
                    except OverflowError:  # Windows C long
                        csv.field_size_limit(2**31 - 1)
                    with path.open(
                        "r", encoding=encoding, errors="strict", newline=""
                    ) as stream:
                        for _ in csv.reader(stream, strict=True):
                            pass
                finally:
                    csv.field_size_limit(old_limit)

    except FileRejected:
        raise
    except (fitz.FileDataError, UnidentifiedImageError, UnicodeError, csv.Error) as exc:
        raise FileRejected(
            "invalid_file_content",
            "File bị hỏng hoặc không thể phân tích theo định dạng đã khai báo.",
        ) from exc
    except Exception as exc:
        # Parser exception messages can contain local paths or document data.
        raise FileRejected(
            "invalid_file_content",
            "File bị hỏng hoặc không thể phân tích theo định dạng đã khai báo.",
        ) from exc


def _validate_saved_document(path: Path, extension: str) -> str:
    """Run all blocking signature/MIME/parser checks in a worker thread."""

    _validate_signature(path, extension)
    detected_mime = detect_mime(path)
    if detected_mime not in DETECTED_MIMES[extension]:
        raise FileRejected(
            "detected_mime_mismatch",
            "MIME nội dung thực tế không khớp đuôi file.",
        )
    _validate_parser(path, extension)
    return detected_mime


async def save_and_validate(upload: UploadFile, workdir: Path) -> StoredDocument:
    original_name = sanitize_filename(upload.filename)
    extension = Path(original_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise FileRejected(
            "unsupported_extension",
            "Định dạng file chưa được hỗ trợ.",
        )

    declared_mime = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if declared_mime not in DECLARED_MIMES[extension]:
        raise FileRejected(
            "declared_mime_mismatch",
            "MIME do trình duyệt khai báo không khớp đuôi file.",
        )

    destination = workdir / f"{uuid4().hex}{extension}"
    try:
        await upload.seek(0)
        with destination.open("xb") as output:
            # There is intentionally no byte counter or byte limit here.
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
    except OSError as exc:
        raise FileRejected(
            "temporary_write_failed",
            "Không thể ghi file vào vùng xử lý tạm.",
        ) from exc

    detected_mime = await anyio.to_thread.run_sync(
        _validate_saved_document,
        destination,
        extension,
    )
    return StoredDocument(
        original_name=original_name,
        path=destination,
        extension=extension,
        declared_mime=declared_mime,
        detected_mime=detected_mime,
    )


def split_text_without_loss(text: str, chunk_chars: int) -> list[str]:
    """Split text on useful boundaries without truncating or skipping a char."""

    if not text:
        return []

    chunks: list[str] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        end = min(cursor + chunk_chars, length)
        if end < length:
            lower_bound = cursor + max(1, chunk_chars // 2)
            newline = text.rfind("\n", lower_bound, end)
            space = text.rfind(" ", lower_bound, end)
            boundary = max(newline, space)
            if boundary > cursor:
                end = boundary + 1
        chunks.append(text[cursor:end])
        cursor = end
    return chunks


def _yield_chunked(
    *,
    filename: str,
    base_location: str,
    text: str,
    chunk_chars: int,
) -> Iterator[ExtractedPart]:
    chunks = split_text_without_loss(text, chunk_chars)
    if not chunks:
        chunks = ["[Không đọc được nội dung; cần người xem]"]
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        suffix = f", đoạn {index}/{total}" if total > 1 else ""
        yield ExtractedPart(
            filename=filename,
            location=f"{base_location}{suffix}",
            text=chunk,
        )


def extract_document(
    document: StoredDocument,
    *,
    chunk_chars: int,
    ocr_lang: str,
) -> Iterator[ExtractedPart]:
    """Yield every page/frame/text chunk; no page or content slice is applied."""

    try:
        if document.extension == ".pdf":
            with fitz.open(document.path) as pdf:
                # Deliberately process range(page_count), never min/slice/break.
                for page_index in range(pdf.page_count):
                    page = pdf.load_page(page_index)
                    embedded_text = page.get_text("text", sort=True).strip()
                    page_area = max(float(page.rect.get_area()), 1.0)
                    large_raster = False
                    for image_info in page.get_image_info():
                        bbox = image_info.get("bbox")
                        if not bbox:
                            continue
                        image_rect = fitz.Rect(bbox) & page.rect
                        if (
                            not image_rect.is_empty
                            and float(image_rect.get_area()) / page_area >= 0.35
                        ):
                            large_raster = True
                            break

                    # OCR empty/sparse pages, plus hybrid pages where a large
                    # raster likely contains more data than the embedded header.
                    should_ocr = len(embedded_text) < 80 or (
                        large_raster and len(embedded_text) < 800
                    )
                    ocr_text = ""
                    if should_ocr:
                        pixmap = page.get_pixmap(dpi=200, alpha=False)
                        with Image.open(BytesIO(pixmap.tobytes("png"))) as image:
                            ocr_text = pytesseract.image_to_string(
                                image,
                                lang=ocr_lang,
                            ).strip()

                    if embedded_text and ocr_text:
                        text = (
                            "[VĂN BẢN NHÚNG]\n"
                            f"{embedded_text}\n"
                            "[VĂN BẢN OCR]\n"
                            f"{ocr_text}"
                        )
                    else:
                        # Never replace useful embedded text with an empty or
                        # lower-quality OCR result.
                        text = embedded_text or ocr_text
                    yield from _yield_chunked(
                        filename=document.original_name,
                        base_location=f"trang {page_index + 1}",
                        text=text,
                        chunk_chars=chunk_chars,
                    )

        elif document.extension in {".png", ".jpg", ".jpeg", ".webp"}:
            with Image.open(document.path) as image:
                for frame_index, frame in enumerate(
                    ImageSequence.Iterator(image), start=1
                ):
                    prepared = ImageOps.exif_transpose(frame.copy()).convert("RGB")
                    try:
                        text = pytesseract.image_to_string(
                            prepared,
                            lang=ocr_lang,
                        ).strip()
                    finally:
                        prepared.close()
                    yield from _yield_chunked(
                        filename=document.original_name,
                        base_location=f"ảnh {frame_index}",
                        text=text,
                        chunk_chars=chunk_chars,
                    )

        elif document.extension == ".xml":
            buffer: list[str] = []
            buffer_size = 0
            part_index = 0
            for _, element in SafeElementTree.iterparse(
                document.path, events=("end",)
            ):
                tag = str(element.tag).rsplit("}", 1)[-1]
                value = (element.text or "").strip()
                lines: list[str] = []
                if value:
                    lines.append(f"{tag}: {value}\n")
                for key, attribute_value in element.attrib.items():
                    clean_key = str(key).rsplit("}", 1)[-1]
                    lines.append(f"{tag}.@{clean_key}: {attribute_value}\n")
                element.clear()

                for line in lines:
                    buffer.append(line)
                    buffer_size += len(line)
                if buffer_size >= chunk_chars:
                    joined = "".join(buffer)
                    for chunk in split_text_without_loss(joined, chunk_chars):
                        part_index += 1
                        yield ExtractedPart(
                            filename=document.original_name,
                            location=f"XML, đoạn {part_index}",
                            text=chunk,
                        )
                    buffer.clear()
                    buffer_size = 0

            if buffer:
                for chunk in split_text_without_loss("".join(buffer), chunk_chars):
                    part_index += 1
                    yield ExtractedPart(
                        filename=document.original_name,
                        location=f"XML, đoạn {part_index}",
                        text=chunk,
                    )
            if part_index == 0:
                yield ExtractedPart(
                    filename=document.original_name,
                    location="XML",
                    text="[XML không có nội dung văn bản; cần người xem]",
                )

        else:
            encoding = _detect_text_encoding(document.path)
            part_index = 0
            with document.path.open(
                "r", encoding=encoding, errors="strict", newline=""
            ) as stream:
                # TextIOWrapper.read(n) reads characters, not bytes. Every
                # character is yielded exactly once.
                while text := stream.read(chunk_chars):
                    part_index += 1
                    yield ExtractedPart(
                        filename=document.original_name,
                        location=f"đoạn {part_index}",
                        text=text,
                    )
            if part_index == 0:
                yield ExtractedPart(
                    filename=document.original_name,
                    location="đoạn 1",
                    text="[File văn bản rỗng; cần người xem]",
                )

    except ExtractionFailed:
        raise
    except Exception as exc:
        # Never expose parser/OCR command output, paths, or document contents.
        raise ExtractionFailed("document extraction failed") from exc
