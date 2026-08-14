from __future__ import annotations

import re

import pytest

import app.service as service_module
from app.errors import StructuredOutputFailed
from app.files import ExtractedPart, StoredDocument
from app.schemas import (
    CheckInput,
    EvidencePacket,
    MapResult,
    ReduceResult,
    ReviewResult,
)
from app.service import ReviewPipeline
from conftest import make_review


EMPTY_EVIDENCE = EvidencePacket(tai_lieu=[], doi_chieu_cheo=[], mau_thuan=[])


class RecordingGateway:
    def __init__(self, events: list[str], *, large_map_output: bool = False):
        self.events = events
        self.large_map_output = large_map_output
        self.reduce_calls = 0

    async def close(self):
        self.events.append("closed-gateway")

    async def generate(self, output_model, *, system_prompt, user_prompt):
        del system_prompt
        if output_model is MapResult:
            match = re.search(r"SOURCE_ID BẮT BUỘC TRẢ LẠI: ([^\n]+)", user_prompt)
            assert match
            source_id = match.group(1).strip()
            part_number = int(source_id.rsplit("p", 1)[1])
            self.events.append(f"map-{part_number}")
            evidence = (
                EvidencePacket(
                    tai_lieu=[],
                    doi_chieu_cheo=["X" * 7_000],
                    mau_thuan=[],
                )
                if self.large_map_output
                else EMPTY_EVIDENCE
            )
            return MapResult(source_id=source_id, evidence=evidence)
        if output_model is ReduceResult:
            self.reduce_calls += 1
            self.events.append("reduce")
            return ReduceResult(evidence=EMPTY_EVIDENCE)
        if output_model is ReviewResult:
            self.events.append("final")
            return make_review(["lazy.txt"])
        raise AssertionError(f"unexpected schema: {output_model}")


def metadata() -> CheckInput:
    return CheckInput(
        eform="FA-PM260721005",
        type="PO/PR",
        is_inv=False,
        item_number="",
        payee="NCC",
    )


def stored(tmp_path) -> StoredDocument:
    path = tmp_path / "lazy.txt"
    path.write_text("unused", encoding="utf-8")
    return StoredDocument(
        original_name="lazy.txt",
        path=path,
        extension=".txt",
        declared_mime="text/plain",
        detected_mime="text/plain",
    )


@pytest.mark.asyncio
async def test_pipeline_consumes_extraction_generator_lazily(
    tmp_path,
    monkeypatch,
    settings_factory,
):
    events: list[str] = []

    def lazy_generator(document, *, chunk_chars, ocr_lang):
        del document, chunk_chars, ocr_lang
        events.append("produce-1")
        yield ExtractedPart("lazy.txt", "đoạn 1", "first")
        assert "map-1" in events
        events.append("produce-2")
        yield ExtractedPart("lazy.txt", "đoạn 2", "second")
        assert "map-2" in events

    monkeypatch.setattr(service_module, "extract_document", lazy_generator)
    gateway = RecordingGateway(events)
    pipeline = ReviewPipeline(gateway, settings_factory())

    result = await pipeline.review(metadata(), [stored(tmp_path)])

    assert result.docs[0].ten_file == "lazy.txt"
    assert events.index("map-1") < events.index("produce-2")
    assert events.index("map-2") < events.index("final")


@pytest.mark.asyncio
async def test_pipeline_closes_generator_when_map_fails(
    tmp_path,
    monkeypatch,
    settings_factory,
):
    events: list[str] = []

    def generator_with_finally(document, *, chunk_chars, ocr_lang):
        del document, chunk_chars, ocr_lang
        try:
            yield ExtractedPart("lazy.txt", "đoạn 1", "first")
            yield ExtractedPart("lazy.txt", "đoạn 2", "second")
        finally:
            events.append("generator-closed")

    class FailingGateway(RecordingGateway):
        async def generate(self, output_model, *, system_prompt, user_prompt):
            if output_model is MapResult:
                raise StructuredOutputFailed("test failure with secret body")
            return await super().generate(
                output_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

    monkeypatch.setattr(service_module, "extract_document", generator_with_finally)
    pipeline = ReviewPipeline(FailingGateway(events), settings_factory())

    with pytest.raises(StructuredOutputFailed):
        await pipeline.review(metadata(), [stored(tmp_path)])

    assert "generator-closed" in events


@pytest.mark.asyncio
async def test_hierarchical_reduce_runs_without_dropping_map_coverage(
    tmp_path,
    monkeypatch,
    settings_factory,
):
    events: list[str] = []

    def five_parts(document, *, chunk_chars, ocr_lang):
        del document, chunk_chars, ocr_lang
        for index in range(5):
            yield ExtractedPart("lazy.txt", f"đoạn {index + 1}", str(index))

    monkeypatch.setattr(service_module, "extract_document", five_parts)
    gateway = RecordingGateway(events, large_map_output=True)
    pipeline = ReviewPipeline(gateway, settings_factory(maas_merge_chars=8_000))

    await pipeline.review(metadata(), [stored(tmp_path)])

    assert events.count("map-1") == 1
    assert events.count("map-5") == 1
    assert gateway.reduce_calls >= 1
    assert events[-1] == "final"

