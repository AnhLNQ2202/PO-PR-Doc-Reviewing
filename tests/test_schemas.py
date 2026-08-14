from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import ReviewResult
from conftest import make_review


def test_result_schema_uses_exact_frontend_fields():
    dumped = make_review(["invoice.pdf"]).model_dump()
    summary = dumped["tong_hop"]

    assert "tru_ns_pr" in summary
    assert set(summary) == {
        "ncc",
        "mst",
        "so_po",
        "so_pr",
        "budget_code",
        "product_code",
        "loai_hang",
        "mo_ta_hang",
        "so_luong",
        "don_gia_truoc_thue",
        "truoc_vat",
        "vat",
        "tong",
        "tru_ns_pr",
        "ngay_hoa_don",
        "ngay_bbbg",
        "hinh_thuc_tt",
        "term_thanh_toan",
        "tk_nhan_tren_unc",
    }


def test_result_rejects_extra_fields():
    data = make_review(["invoice.pdf"]).model_dump()
    data["unexpected"] = "not allowed"

    with pytest.raises(ValidationError):
        ReviewResult.model_validate(data)


def test_result_rejects_invalid_rule_status():
    data = make_review(["invoice.pdf"]).model_dump()
    data["docs"][0]["rules"][0]["trang_thai"] = "ok"

    with pytest.raises(ValidationError):
        ReviewResult.model_validate(data)


def test_result_rejects_negative_money_and_bad_product_code():
    data = make_review(["invoice.pdf"]).model_dump()
    data["tong_hop"]["tong"] = -1.0
    data["tong_hop"]["product_code"] = "IT.Monitor"

    with pytest.raises(ValidationError):
        ReviewResult.model_validate(data)


def test_result_rejects_unknown_and_duplicate_business_rule_ids():
    unknown = make_review(["invoice.pdf"]).model_dump()
    unknown["docs"][0]["rules"][0]["id"] = "ZZ9"
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(unknown)

    duplicate = make_review(["invoice.pdf", "po.pdf"]).model_dump()
    duplicate["docs"][0]["rules"][0]["id"] = "A1"
    duplicate["docs"][1]["rules"][0]["id"] = "a1"
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(duplicate)


def test_result_validates_invoice_arithmetic_with_one_vnd_tolerance():
    within_tolerance = make_review(["invoice.pdf"]).model_dump()
    within_tolerance["tong_hop"]["tong"] = 110_001.0
    accepted = ReviewResult.model_validate(within_tolerance)
    assert accepted.tong_hop.tong == 110_001.0

    inconsistent = make_review(["invoice.pdf"]).model_dump()
    inconsistent["tong_hop"]["tong"] = 110_002.0
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(inconsistent)


def test_result_does_not_reject_when_only_one_total_is_known():
    partial = make_review(["invoice.pdf"]).model_dump()
    partial["tong_hop"]["truoc_vat"] = 0.0
    partial["tong_hop"]["vat"] = 0.0
    partial["tong_hop"]["tong"] = 110_000.0

    assert ReviewResult.model_validate(partial).tong_hop.tong == 110_000.0
