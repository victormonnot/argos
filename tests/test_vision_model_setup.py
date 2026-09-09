"""Explicit, bounded model installation and offline cache integrity."""
import hashlib
from dataclasses import replace
import io
from pathlib import Path
import sys

import pytest

from argos.perception import yolox
from examples import setup_vision_model as setup


@pytest.fixture
def model_bytes(monkeypatch):
    data = b"small model fixture, not inference weights"
    monkeypatch.setattr(yolox, "MODEL_CATALOG", {
        key: replace(spec, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        for key, spec in yolox.MODEL_CATALOG.items()})
    return data


def test_verified_model_is_reused_without_network(tmp_path, monkeypatch, model_bytes):
    output = tmp_path / "model.onnx"
    output.write_bytes(model_bytes)
    monkeypatch.setattr(setup, "urlopen", lambda *a, **k: pytest.fail("verified cache is offline"))
    assert setup.install(output) == output
    assert "Apache License, Version 2.0" in output.with_suffix(".NOTICE.txt").read_text()


def test_download_has_exact_url_size_bound_and_atomic_cleanup(tmp_path, monkeypatch, model_bytes):
    requested = []
    class Download(io.BytesIO):
        def read(self, length):
            requested.append(length)
            return super().read(length)
    def fetch(url, *, timeout):
        requested.append((url, timeout))
        return Download(model_bytes)
    monkeypatch.setattr(setup, "urlopen", fetch)
    output = tmp_path / "models" / "model.onnx"
    assert setup.install(output) == output
    assert output.read_bytes() == model_bytes
    assert requested == [(setup.MODEL_URL, 30), len(model_bytes) + 1]
    assert "/0.1.1rc0/" in setup.MODEL_URL
    assert not list(output.parent.glob(".yolox-*"))


@pytest.mark.parametrize("corrupt", [b"", b"wrong", b"x" * 100])
def test_bad_download_does_not_replace_an_existing_file(tmp_path, monkeypatch, model_bytes, corrupt):
    output = tmp_path / "model.onnx"
    output.write_bytes(b"previous file")
    monkeypatch.setattr(setup, "urlopen", lambda *a, **kw: io.BytesIO(corrupt))
    with pytest.raises(ValueError, match="size/SHA-256"):
        setup.install(output)
    assert output.read_bytes() == b"previous file"
    assert not list(tmp_path.glob(".yolox-*"))


def test_failed_atomic_replace_cleans_temporary_and_preserves_old_file(tmp_path, monkeypatch, model_bytes):
    output = tmp_path / "model.onnx"
    output.write_bytes(b"previous file")
    monkeypatch.setattr(setup, "urlopen", lambda *a, **kw: io.BytesIO(model_bytes))
    def denied(*args):
        raise OSError("replacement failed")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(OSError, match="replacement failed"):
        setup.install(output)
    assert output.read_bytes() == b"previous file"
    assert not list(tmp_path.glob(".yolox-*"))


def test_relative_xdg_cache_is_ignored(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")
    assert yolox.default_model_path() == Path.home() / ".cache/argos/models/yolox_tiny.onnx"
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/argos-cache-test")
    assert yolox.default_model_path() == Path("/tmp/argos-cache-test/argos/models/yolox_tiny.onnx")


def test_explicit_output_is_used_by_cli(tmp_path, monkeypatch, capsys):
    output = tmp_path / "chosen.onnx"
    seen = []
    monkeypatch.setattr(sys, "argv", ["setup_vision_model.py", "--output", str(output)])
    monkeypatch.setattr(setup, "install", lambda path, *, variant: seen.append((path, variant)) or path)
    setup.main()
    assert seen == [(output, "tiny")]
    assert str(output) in capsys.readouterr().out


def test_s_variant_uses_its_own_download_notice_and_cache(tmp_path, monkeypatch, model_bytes):
    calls = []
    def fetch(url, *, timeout):
        calls.append(url)
        return io.BytesIO(model_bytes)
    monkeypatch.setattr(setup, "urlopen", fetch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    output = yolox.default_model_path("s")
    assert output.name == "yolox_s.onnx"
    setup.install(output, variant="s")
    assert calls == [yolox.get_model_spec("s").url]
    notice = output.with_suffix(".NOTICE.txt").read_text()
    assert "YOLOX-S ONNX" in notice and "Input: 640 x 640; variant: s" in notice
    setup.install(output, variant="s")
    assert len(calls) == 1  # Checked S cache remains offline, too.


def test_s_cli_selects_s_default_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["setup_vision_model.py", "--variant", "s"])
    seen = []
    monkeypatch.setattr(setup, "install", lambda path, *, variant: seen.append((path, variant)) or path)
    setup.main()
    assert seen == [(tmp_path / "argos/models/yolox_s.onnx", "s")]
    assert "Verified YOLOX-S" in capsys.readouterr().out
