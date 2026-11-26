import pytest

from src.stats.collector import CONTROLLED_ERROR_TYPES, _normalize_error_type


@pytest.mark.parametrize("code", ["service_paused", "invalid_request"])
def test_local_rejection_type_is_preserved_by_collector(code):
    assert code in CONTROLLED_ERROR_TYPES
    assert _normalize_error_type(code) == code


def test_unknown_error_type_is_still_rejected():
    assert _normalize_error_type("uncontrolled_pause_reason") is None
