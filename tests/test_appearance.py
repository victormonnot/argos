"""Bounded appearance metadata; image fixtures are synthetic and local."""
import builtins
import math

import pytest

from argos.perception.appearance import (
    AppearanceEncoder, DESCRIPTOR_SIZE, similarity, validate_appearances,
    validate_descriptor,
)


def vector():
    return [1.] + [0.] * (DESCRIPTOR_SIZE - 1)


def test_descriptor_validation_copies_finite_unit_vectors_and_preserves_none():
    source = vector()
    result = validate_appearances([source, None], 2)
    source[0] = 0.
    assert result == [tuple(vector()), None]
    assert similarity(result[0], result[0]) == 1
    assert similarity(result[0], None) is None
    assert validate_descriptor(None) is None


@pytest.mark.parametrize("bad", [[], [0.] * 208, [1.] * 208,
    [True] + [0.] * 207, [float("nan")] + [0.] * 207,
    [float("inf")] + [0.] * 207, [-1.] + [0.] * 207,
    [10 ** 400] + [0.] * 207, {"values": vector()}, "x" * 208])
def test_invalid_descriptors_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_descriptor(bad)


@pytest.mark.parametrize("values,count", [(None, 1), ([None], 0), ([], 1),
                                         ([None] * 17, 17), ((None,), 1)])
def test_descriptor_alignment_and_count_are_bounded(values, count):
    with pytest.raises(ValueError):
        validate_appearances(values, count)


def test_opencv_is_optional_until_encoder_construction(monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("test unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert validate_descriptor(vector()) == tuple(vector())
    with pytest.raises(RuntimeError, match="optional ARGOS camera"):
        AppearanceEncoder()


@pytest.mark.parametrize("jpeg,detections,width,height", [
    (b"x", [], True, 100), (b"x", [], 100, 0), (b"x", [], 4097, 100),
    (b"", [], 100, 100), (bytearray(b"x"), [], 100, 100),
    (b"x" * (16 * 1024 * 1024 + 1), [], 100, 100),
    (b"x", [None], 100, 100), (b"x", None, 100, 100),
    (b"x", [{"box": [0, 0, .1, .1]}] * 17, 100, 100),
    (b"x", [{"box": [0, 0, 0, .1]}], 100, 100),
    (b"x", [{"box": [True, 0, .1, .1]}], 100, 100),
    (b"x", [{"box": [.9, 0, .2, .1]}], 100, 100),
    (b"x", [{"box": [0, 0, float("nan"), .1]}], 100, 100),
])
def test_invalid_inputs_fail_before_image_decode(jpeg, detections, width, height):
    encoder = AppearanceEncoder.__new__(AppearanceEncoder)
    # No CV/NumPy attributes: reaching the decoder would fail with AttributeError.
    with pytest.raises(ValueError):
        encoder.encode(jpeg, detections, width=width, height=height)


def encoded_image():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image = np.full((240, 320, 3), 155, dtype=np.uint8)
    image[48:120, 80:128] = [45, 60, 200]
    image[120:192, 80:128] = [100, 45, 30]
    ok, data = cv2.imencode(".jpg", image)
    assert ok
    return cv2, np, image, data.tobytes()


def test_encoder_output_is_aligned_and_bounded_for_visible_clipped_and_tiny_boxes():
    _, _, _, data = encoded_image()
    encoder = AppearanceEncoder()
    result = encoder.encode(data, [
        {"box": [.25, .2, .15, .6]},
        {"box": [0, .2, .15, .6]},
        {"box": [.25, .2, .01, .1]},
    ], width=320, height=240)
    assert isinstance(result[0], tuple) and len(result[0]) == 208
    assert math.isclose(sum(value * value for value in result[0]), 1., abs_tol=1e-10)
    assert result[1:] == [None, None]


def test_translated_crop_keeps_appearance_without_using_previous_image_state():
    cv2, np, image, data = encoded_image()
    encoder = AppearanceEncoder()
    first = encoder.encode(data, [{"box": [.25, .2, .15, .6]}], width=320, height=240)[0]
    moved = np.full_like(image, 155)
    moved[48:192, 176:224] = image[48:192, 80:128]
    _, encoded = cv2.imencode(".jpg", moved)
    second = encoder.encode(encoded.tobytes(), [{"box": [.55, .2, .15, .6]}], width=320, height=240)[0]
    assert similarity(first, second) > .95


def test_uniform_crop_has_no_appearance_evidence():
    cv2, np, image, _ = encoded_image()
    image[:] = 0
    _, encoded = cv2.imencode(".jpg", image)
    assert AppearanceEncoder().encode(encoded.tobytes(), [{"box": [.25, .2, .15, .6]}],
                                      width=320, height=240) == [None]


def test_decode_failure_and_dimension_mismatch_are_not_silent_missing_descriptors():
    _, _, _, data = encoded_image()
    encoder = AppearanceEncoder()
    for jpeg, width, height in [(b"not an image", 320, 240), (data, 319, 240), (data, 240, 320)]:
        with pytest.raises(ValueError, match="dimensions differ"):
            encoder.encode(jpeg, [], width=width, height=height)
