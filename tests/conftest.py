from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.schemas import (  # noqa: E402
    DocumentResult,
    ReviewResult,
    RuleResult,
    Summary,
)


def make_summary() -> Summary:
    return Summary(
        ncc="Công ty Demo",
        mst="0100000000",
        so_po="PO-001",
        so_pr="PR-001",
        budget_code="MAREXPE.1000",
        product_code="000",
        loai_hang="khac",
        mo_ta_hang="Hàng demo",
        so_luong="1 chiếc",
        don_gia_truoc_thue=100_000.0,
        truoc_vat=100_000.0,
        vat=10_000.0,
        tong=110_000.0,
        tru_ns_pr=100_000.0,
        ngay_hoa_don="01/08/2026",
        ngay_bbbg="01/08/2026",
        hinh_thuc_tt="Chuyển khoản",
        term_thanh_toan="NET30",
        tk_nhan_tren_unc="",
    )


def make_review(filenames: list[str]) -> ReviewResult:
    return ReviewResult(
        tong_hop=make_summary(),
        docs=[
            DocumentResult(
                ten_file=filename,
                loai="KHAC",
                fields={"Tên file": filename},
                rules=[
                    RuleResult(
                        id="K1",
                        ten="Đã đọc file",
                        trang_thai="xem",
                        chi_tiet="Dữ liệu kiểm thử.",
                    )
                ],
            )
            for filename in filenames
        ],
    )


@pytest.fixture
def settings_factory(tmp_path):
    def factory(**overrides) -> Settings:
        values = {
            "maas_api_key": "test-secret-key",
            "maas_base_url": "https://maas.invalid/v1",
            "maas_model": "test-model",
            "maas_timeout_seconds": 30,
            "maas_max_output_tokens": 8_000,
            "maas_chunk_chars": 2_000,
            "maas_merge_chars": 8_000,
            "maas_json_mode": "none",
            "ocr_lang": "vie+eng",
            "temp_dir": tmp_path / "request-temp",
            "app_version": "test",
            "stale_temp_seconds": 0,
        }
        values.update(overrides)
        return Settings(**values)

    return factory

