import pytest

from src.compat.openai.request import InvalidRequest
from src.media.request import parse_media_request


def body(**values):
    return {"model": "gem-3.1", "prompt": "hello", **values}


def test_minimal_image_and_optional_user_are_preserved_without_extra_upstream_fields():
    assert parse_media_request(body(model=" gem-3.1 ", user="client"), "image") == {
        "model": "gem-3.1",
        "prompt": "hello",
        "kind": "image",
        "options": {"count": 1},
        "response_format": "url",
    }
    assert parse_media_request(body(n=4, response_format="b64_json", size="auto"), "image")[
        "options"
    ] == {"count": 4}


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        body(seed=1),
        body(prompt=None),
        body(prompt="  "),
        body(prompt="a" * 100001),
        body(model=None),
        body(model=" "),
        body(model="a" * 257),
        body(n=True),
        body(n=0),
        body(n=5),
        body(response_format="other"),
    ],
)
def test_malformed_or_unimplemented_requests_are_rejected(invalid):
    with pytest.raises(InvalidRequest):
        parse_media_request(invalid, "image")


@pytest.mark.parametrize("field", ["aspect_ratio", "resolution"])
@pytest.mark.parametrize("value", [None, 1, " ", "x" * 33])
def test_option_strings_must_be_short(field, value):
    with pytest.raises(InvalidRequest):
        parse_media_request(body(**{field: value}), "image")


@pytest.mark.parametrize(
    "size", [1, "1024X1024", "0x1024", "10x0", "8x8", "1024x999999", "512x512"]
)
def test_invalid_or_unsupported_image_sizes_are_not_silently_ignored(size):
    with pytest.raises(InvalidRequest):
        parse_media_request(body(size=size), "image")


@pytest.mark.parametrize(
    "size, ratio, resolution",
    [
        ("1024x1024", "1:1", "1K"),
        ("1536x1024", "3:2", "1.5K"),
        ("2048x1024", "2:1", "2K"),
        ("4096x2048", "2:1", "4K"),
    ],
)
def test_image_size_maps_only_to_supported_protocol_geometry(size, ratio, resolution):
    assert parse_media_request(body(size=size), "image")["options"] == {
        "count": 1,
        "aspect_ratio": ratio,
        "resolution": resolution,
    }


def test_explicit_native_options_override_geometry_defaults():
    assert parse_media_request(
        body(size="1024x1024", aspect_ratio="16:9", resolution="2K"), "image"
    )["options"] == {
        "count": 1,
        "aspect_ratio": "16:9",
        "resolution": "2K",
    }


def test_video_size_and_numeric_string_duration_map_to_actual_protocol():
    parsed = parse_media_request(body(size="1280x720", seconds="1", generate_audio=False), "video")
    assert parsed["options"] == {
        "count": 1,
        "aspect_ratio": "16:9",
        "resolution": "720P",
        "duration": 1,
        "generate_audio": False,
    }
    assert parse_media_request(body(duration=5), "video")["options"]["duration"] == 5
    assert parse_media_request(body(seconds=1, duration=5), "video")["options"]["duration"] == 1


@pytest.mark.parametrize(
    "values",
    [
        {"duration": True},
        {"duration": 0},
        {"duration": 61},
        {"duration": "bad"},
        {"duration": "²"},
        {"duration": "9" * 5000},
        {"duration": 1.5},
        {"generate_audio": "false"},
        {"n": 2},
        {"response_format": "b64_json"},
    ],
)
def test_invalid_video_options_are_rejected(values):
    with pytest.raises(InvalidRequest):
        parse_media_request(body(**values), "video")


@pytest.mark.parametrize("values", [{"seconds": 1}, {"generate_audio": False}])
def test_video_only_options_cannot_be_sent_to_image_generation(values):
    with pytest.raises(InvalidRequest):
        parse_media_request(body(**values), "image")
