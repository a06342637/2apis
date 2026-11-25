"""媒体元数据透传和跨重启目录恢复保持实际调用方式。"""

from src.api.model_catalog import load_catalog, save_catalog
from src.api.model_merge import _entry_response, _merge_provider
from src.provider.base import Model


def test_media_catalog_roundtrip(tmp_path):
    entry = Model(id="image-model", gateway_endpoints=("/v1/images/generations",),
                  output_modalities=("image",), supports_tool_call=False)
    save_catalog(str(tmp_path), {"miora": {entry.id: entry}})
    restored = load_catalog(str(tmp_path))["miora"][1][entry.id]
    assert restored == entry


def test_media_metadata_in_provider_and_merged_entry():
    model = Model(id="gem-3.1", name="Gem 3.1", gateway_endpoints=("/v1/images/generations",),
                  output_modalities=("image",), supports_tool_call=False)
    entries = {}
    _merge_provider(entries, "miora", {model.id: model})
    entry = next(iter(entries.values()))
    entry["id"] = "gem-3.1"
    response = _entry_response(entry)
    assert response["gateway_endpoints"] == ("/v1/images/generations",)
    assert response["by_provider"]["miora"]["output_modalities"] == ("image",)
