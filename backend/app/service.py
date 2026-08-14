from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator, cast

import anyio

from .config import Settings
from .errors import ExtractionFailed, StructuredOutputFailed
from .files import ExtractedPart, StoredDocument, extract_document
from .maas import StructuredGateway
from .prompts import (
    UNTRUSTED_DATA_SYSTEM,
    final_prompt,
    map_prompt,
    reduce_prompt,
)
from .schemas import (
    CheckInput,
    DocumentResult,
    EvidencePacket,
    MapResult,
    ReduceResult,
    ReviewResult,
    RuleResult,
)


logger = logging.getLogger(__name__)
_END_OF_PARTS = object()


def _next_or_sentinel(
    iterator: Iterator[ExtractedPart],
) -> ExtractedPart | object:
    """Do not let StopIteration cross an async Future boundary."""

    try:
        return next(iterator)
    except StopIteration:
        return _END_OF_PARTS


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    evidence: EvidencePacket
    covered_parts: int

    @property
    def json(self) -> str:
        return self.evidence.model_dump_json()

    @property
    def char_count(self) -> int:
        return len(self.json)


class ReviewPipeline:
    def __init__(self, gateway: StructuredGateway, settings: Settings):
        self._gateway = gateway
        self._settings = settings

    async def close(self) -> None:
        await self._gateway.close()

    async def _map_part(
        self,
        metadata: CheckInput,
        part: ExtractedPart,
        source_id: str,
    ) -> EvidenceEnvelope:
        mapped = await self._gateway.generate(
            MapResult,
            system_prompt=UNTRUSTED_DATA_SYSTEM,
            user_prompt=map_prompt(
                metadata,
                source_id=source_id,
                filename=part.filename,
                location=part.location,
                content=part.text,
            ),
        )
        if mapped.source_id != source_id:
            raise StructuredOutputFailed("map source coverage mismatch")
        return EvidenceEnvelope(evidence=mapped.evidence, covered_parts=1)

    @staticmethod
    def _total_chars(envelopes: list[EvidenceEnvelope]) -> int:
        return sum(item.char_count for item in envelopes)

    def _groups_for_reduce(
        self,
        envelopes: list[EvidenceEnvelope],
    ) -> list[list[EvidenceEnvelope]]:
        groups: list[list[EvidenceEnvelope]] = []
        current: list[EvidenceEnvelope] = []
        current_chars = 0

        for envelope in envelopes:
            size = envelope.char_count
            if current and current_chars + size > self._settings.maas_merge_chars:
                groups.append(current)
                current = []
                current_chars = 0
            current.append(envelope)
            current_chars += size
        if current:
            groups.append(current)

        # If every packet alone exceeds the target, pair them anyway so the
        # hierarchy still makes progress. No packet is discarded or sliced.
        if len(groups) == len(envelopes) and len(envelopes) > 1:
            groups = [envelopes[index : index + 2] for index in range(0, len(envelopes), 2)]
        return groups

    async def _reduce_group(
        self,
        metadata: CheckInput,
        group: list[EvidenceEnvelope],
    ) -> EvidenceEnvelope:
        coverage = sum(item.covered_parts for item in group)
        payload = json.dumps(
            [item.evidence.model_dump(mode="json") for item in group],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        reduced = await self._gateway.generate(
            ReduceResult,
            system_prompt=UNTRUSTED_DATA_SYSTEM,
            user_prompt=reduce_prompt(metadata, payload, coverage),
        )
        return EvidenceEnvelope(
            evidence=reduced.evidence,
            covered_parts=coverage,
        )

    async def _compact_to_budget(
        self,
        metadata: CheckInput,
        envelopes: list[EvidenceEnvelope],
    ) -> list[EvidenceEnvelope]:
        current = envelopes
        while (
            len(current) > 1
            and self._total_chars(current) > self._settings.maas_merge_chars
        ):
            groups = self._groups_for_reduce(current)
            next_level: list[EvidenceEnvelope] = []
            for group in groups:
                if len(group) == 1:
                    next_level.append(group[0])
                else:
                    next_level.append(await self._reduce_group(metadata, group))
            if len(next_level) >= len(current):
                raise StructuredOutputFailed("evidence hierarchy made no progress")
            current = next_level
        return current

    async def review(
        self,
        metadata: CheckInput,
        documents: list[StoredDocument],
    ) -> ReviewResult:
        envelopes: list[EvidenceEnvelope] = []
        mapped_part_count = 0

        for document_index, document in enumerate(documents, start=1):
            parts = extract_document(
                document,
                chunk_chars=self._settings.maas_chunk_chars,
                ocr_lang=self._settings.ocr_lang,
            )
            part_index = 0
            try:
                while True:
                    # Pull exactly one chunk in a worker. The next chunk is not
                    # read/OCRed until the current chunk has completed MAP and
                    # any required compaction.
                    next_part = await anyio.to_thread.run_sync(
                        _next_or_sentinel,
                        parts,
                    )
                    if next_part is _END_OF_PARTS:
                        break
                    part = cast(ExtractedPart, next_part)
                    part_index += 1
                    mapped_part_count += 1
                    source_id = f"d{document_index:06d}-p{part_index:09d}"
                    envelopes.append(
                        await self._map_part(metadata, part, source_id)
                    )

                    # Compact incrementally so a request with arbitrarily many
                    # pages does not retain all map responses in memory.
                    if self._total_chars(envelopes) > self._settings.maas_merge_chars * 2:
                        envelopes = await self._compact_to_budget(metadata, envelopes)
            except ExtractionFailed:
                raise
            finally:
                # fitz/Pillow/text streams held by the generator are closed
                # before TemporaryDirectory cleanup, including cancellation.
                await anyio.to_thread.run_sync(parts.close)

        if not envelopes or mapped_part_count == 0:
            raise ExtractionFailed("no extracted document parts")

        envelopes = await self._compact_to_budget(metadata, envelopes)
        coverage = sum(item.covered_parts for item in envelopes)
        if coverage != mapped_part_count:
            raise StructuredOutputFailed("evidence coverage mismatch")

        final_evidence = json.dumps(
            [item.evidence.model_dump(mode="json") for item in envelopes],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        filenames = [document.original_name for document in documents]
        result = await self._gateway.generate(
            ReviewResult,
            system_prompt=UNTRUSTED_DATA_SYSTEM,
            user_prompt=final_prompt(metadata, final_evidence, filenames),
        )
        return self._post_process(result, metadata, filenames)

    @staticmethod
    def _post_process(
        result: ReviewResult,
        metadata: CheckInput,
        filenames: list[str],
    ) -> ReviewResult:
        known_names = set(filenames)
        seen_names: set[str] = set()
        for document in result.docs:
            if document.ten_file not in known_names:
                raise StructuredOutputFailed("final output contains unknown filename")
            if document.ten_file in seen_names:
                raise StructuredOutputFailed("final output contains duplicate filename")
            seen_names.add(document.ten_file)
            document.rules = [
                rule
                for rule in document.rules
                if rule.id != "E1" and not (rule.id == "X3" and metadata.payee != "NV")
            ]

        # A missing document is represented explicitly as "cần xem" instead
        # of being silently dropped from the UI.
        for filename in filenames:
            if filename not in seen_names:
                result.docs.append(
                    DocumentResult(
                        ten_file=filename,
                        loai="KHAC",
                        fields={"Ghi chú": "AI chưa tạo kết quả riêng cho file này."},
                        rules=[
                            RuleResult(
                                id="K1",
                                ten="File cần người xem",
                                trang_thai="xem",
                                chi_tiet="File có trong bộ nhưng chưa đủ bằng chứng để phân loại tự động.",
                            )
                        ],
                    )
                )
        return result
