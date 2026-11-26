"""账号权限来自官方目录字段，绝不从价格或模型名称推断。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.provider.miora.access import model_access
from src.provider.miora.client import catalog_models
from src.provider.miora.media import parse_catalog
from tests.provider.miora.test_client import RAW, TIERS


@pytest.mark.parametrize("value", [None, 1, "false", {}, ""])
def test_missing_or_invalid_permissions_are_unknown(value):
    assert model_access(
        {"available": value, "requiredTier": value, "unavailableReason": value}
    ) == {
        "account_available": None,
        "required_account_tier": "false" if value == "false" else None,
        "account_unavailable_reason": "false" if value == "false" else None,
    }


def test_permissions_are_inherited_and_explicit_variant_values_win():
    locked = {
        "available": False,
        "requiredTier": " standard ",
        "unavailableReason": " tier_locked ",
    }
    assert model_access({}, locked) == {
        "account_available": False,
        "required_account_tier": "standard",
        "account_unavailable_reason": "tier_locked",
    }
    assert model_access(
        {"available": True, "requiredTier": "free", "unavailableReason": ""}, locked
    ) == {
        "account_available": True,
        "required_account_tier": "free",
        "account_unavailable_reason": None,
    }
    assert model_access({"billingFactor": 100, "priceTier": "expensive"}) == {
        "account_available": None,
        "required_account_tier": None,
        "account_unavailable_reason": None,
    }


@pytest.mark.parametrize("available", [None, "false", 0, [], {}])
def test_invalid_child_availability_cannot_override_explicit_parent_denial(available):
    assert (
        model_access({"available": available}, {"available": False})["account_available"] is False
    )
    raw = copy.deepcopy(RAW)
    raw["models"]["_meta"]["codebuddy.ai"]["availableModels"][1]["available"] = available
    tiers = copy.deepcopy(TIERS)
    tiers[1]["available"] = False
    models, selections = catalog_models(raw, tiers)
    assert next(model for model in models if model.id == "primary-model").account_available is False
    assert "primary-model" not in selections


def test_observed_free_account_keeps_vip_display_separate_from_generation():
    fixture = Path(__file__).parent / "fixtures" / "media-catalog.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    display = parse_catalog(data, include_unavailable=True)
    usable = parse_catalog(data)
    locked = {key for key, model in display.items() if model.account_available is False}
    assert locked == {
        "minimax-h3",
        "seedance-2.5",
        "kling-video-v4",
        "seedance-2.0-fast",
        "wan-3.0",
        "seedance-2.0",
        "kling-3.0-video",
        "hailuo-2.3-video",
        "vidu-q3-pro-video",
    }
    assert all(display[key].required_account_tier == "standard" for key in locked)
    assert all(display[key].account_unavailable_reason == "tier_locked" for key in locked)
    assert not locked & set(usable)
    assert {key for key, model in usable.items() if model.kind == "video"} == {
        "minimax-h3-max",
        "vidu-q2-pro-video",
    }
    assert usable["gpt-image-2.6-flare-max"].required_account_tier is None
    assert display["minimax-h3"].model().account_available is False
    assert display["minimax-h3"].model().required_account_tier == "standard"


def test_variants_inherit_locked_parent_and_full_display_preserves_them():
    data = {
        "data": [
            {
                "modelId": "parent",
                "visible": True,
                "contentType": "image",
                "available": False,
                "requiredTier": "standard",
                "unavailableReason": "tier_locked",
                "capabilities": {
                    "supportedTools": ["text_to_image"],
                    "variants": [{"modelId": "child", "isDefault": True}],
                },
            }
        ]
    }
    assert parse_catalog(data) == {}
    display = parse_catalog(data, include_unavailable=True)
    assert display["child"].account_available is False
    assert display["child"].required_account_tier == "standard"
    assert display["parent"].raw_id == "child"


def test_text_access_is_only_declared_metadata_and_locked_ids_cannot_be_selected():
    raw = copy.deepcopy(RAW)
    raw["models"]["_meta"]["codebuddy.ai"]["availableModels"][0].update(
        {"available": False, "requiredTier": "standard", "unavailableReason": "tier_locked"}
    )
    tiers = copy.deepcopy(TIERS)
    tiers[1]["available"] = False
    tiers[1]["requiredTier"] = "standard"
    tiers[1]["unavailableReason"] = "tier_locked"
    models, selections = catalog_models(raw, tiers)
    assert not selections
    assert all(model.account_available is False for model in models)
    assert all(model.required_account_tier == "standard" for model in models)
    raw = copy.deepcopy(RAW)
    raw["models"]["_meta"]["codebuddy.ai"]["availableModels"][1]["available"] = True
    models, selections = catalog_models(raw, tiers)
    assert "primary-model" in selections and "miora-pro" not in selections
    assert next(model for model in models if model.id == "miora-pro").account_available is False
    raw = copy.deepcopy(RAW)
    tiers[1]["available"] = True
    models, selections = catalog_models(raw, tiers)
    assert "primary-model" in selections and "miora-pro" in selections
    assert next(model for model in models if model.id == "hy4flash-free").account_available is None
