"""Offline dependency integrity and the visual-only example scene boundary."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from examples import run_web_control as launcher
from examples import setup_vision_scene as setup


@pytest.fixture
def mesh_bytes(monkeypatch):
    content = b"example mesh bytes used only by the download-integrity tests"
    monkeypatch.setattr(setup, "ASSET_BYTES", len(content))
    monkeypatch.setattr(setup, "ASSET_SHA256", hashlib.sha256(content).hexdigest())
    return content


def test_missing_mesh_does_not_download(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("launch-time verification must stay offline")
    monkeypatch.setattr(setup, "urlopen", unexpected)
    with pytest.raises(ValueError, match="setup_vision_scene.py"):
        setup.verified_mesh(tmp_path)


def test_install_is_pinned_and_reuses_verified_cache(tmp_path, monkeypatch, mesh_bytes):
    requests = []
    def fetch(url, *, timeout):
        requests.append((url, timeout))
        return io.BytesIO(mesh_bytes)
    monkeypatch.setattr(setup, "urlopen", fetch)
    installed = setup.install(tmp_path)
    assert installed.read_bytes() == mesh_bytes
    assert "Creative Commons Attribution 4.0" in installed.with_name("NOTICE.txt").read_text()
    assert setup.install(tmp_path) == installed
    assert requests == [(setup.ASSET_URL, 30)]
    assert "/1/files/" in setup.ASSET_URL
    assert not list(installed.parent.glob(".walk-*"))


@pytest.mark.parametrize("corrupt", [b"", b"different", b"x" * 100])
def test_bad_download_preserves_existing_file(tmp_path, monkeypatch, mesh_bytes, corrupt):
    mesh = tmp_path / "argos_walking_person" / "walk.dae"
    mesh.parent.mkdir()
    mesh.write_bytes(b"existing bad file, retained for inspection")
    monkeypatch.setattr(setup, "urlopen", lambda *a, **kw: io.BytesIO(corrupt))
    with pytest.raises(ValueError, match="size/SHA-256"):
        setup.install(tmp_path)
    assert mesh.read_bytes() == b"existing bad file, retained for inspection"
    assert not list(mesh.parent.glob(".walk-*"))


def test_tampered_same_size_mesh_is_rejected(tmp_path, mesh_bytes):
    mesh = tmp_path / "argos_walking_person" / "walk.dae"
    mesh.parent.mkdir()
    mesh.write_bytes(b"x" * len(mesh_bytes))
    with pytest.raises(ValueError, match="checksum differs"):
        setup.verified_mesh(tmp_path)


def test_cache_root_requires_absolute_xdg_path(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "relative-cache")
    assert setup.default_assets_dir() == Path.home() / ".cache/argos/gazebo"
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/argos-test-cache")
    assert setup.default_assets_dir() == Path("/tmp/argos-test-cache/argos/gazebo")


def test_scene_adds_only_rendered_actor_and_private_asset(tmp_path):
    # The simulator route must not add a pose publisher, truth box sensor,
    # camera-follow plugin or vehicle controller alongside the visual actor.
    source = tmp_path / "original.dae"
    source.write_bytes(b"mesh copy")
    models = tmp_path / "models"
    models.mkdir()
    world = ET.fromstring('<world name="iris_runway"><include><uri>model://iris_with_gimbal</uri></include></world>')
    original = ET.tostring(world[0])
    launcher.add_person_scene(world, models, source)
    assert ET.tostring(world[0]) == original
    assert [child.tag for child in world] == ["include", "actor"]
    actor = world.find("actor")
    assert actor is not None
    assert [child.tag for child in actor] == ["skin", "animation", "script"]
    assert actor.find(".//sensor") is None
    assert actor.find(".//plugin") is None
    assert source.read_bytes() == b"mesh copy"
    assert (models / "argos_walking_person/walk.dae").read_bytes() == b"mesh copy"
    assert (models / "argos_walking_person/NOTICE.txt").is_file()
    poses = [point.findtext("pose") for point in actor.findall(".//waypoint")]
    assert len(set(poses)) > 2
    assert poses[0] == poses[-1]
    assert {node.text for node in actor.findall(".//filename")} == {"model://argos_walking_person/walk.dae"}


def test_person_camera_copy_removes_only_zoom_and_preserves_optics(tmp_path):
    gazebo = tmp_path / "upstream"
    original = gazebo / "models/gimbal_small_3d/model.sdf"
    original.parent.mkdir(parents=True)
    content = '''<sdf version="1.9"><model name="gimbal_small_3d">
      <pose>0 0 .18 0 0 0</pose><joint name="pitch_joint" type="revolute" />
      <link name="pitch_link"><sensor name="camera" type="camera">
        <pose>0 0 0 -1.57 -1.57 0</pose>
        <camera><horizontal_fov>1.2</horizontal_fov>
          <image><width>640</width><height>480</height></image></camera>
        <plugin filename="CameraZoomPlugin" name="CameraZoomPlugin" />
        <plugin filename="GstCameraPlugin" name="GstCameraPlugin" />
      </sensor></link></model></sdf>'''
    original.write_text(content)
    (original.parent / "mesh.dae").write_bytes(b"unchanged camera mesh")
    models = tmp_path / "private-models"
    models.mkdir()
    launcher.copy_fixed_camera(gazebo, models)
    assert original.read_text() == content
    expected = ET.fromstring(content)
    sensor = expected.find(".//sensor")
    sensor.remove(sensor.find("plugin[@filename='CameraZoomPlugin']"))
    copied = ET.parse(models / "gimbal_small_3d/model.sdf").getroot()
    assert ET.tostring(copied) == ET.tostring(expected)
    assert (models / "gimbal_small_3d/mesh.dae").read_bytes() == b"unchanged camera mesh"
