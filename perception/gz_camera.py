"""gz_camera.py — lit le flux de la camera Gazebo (topic image gz-transport) en BGR.

Abonnement direct au topic image du capteur (pas de H264/RTP) via les bindings Python
gz-transport. Garde la derniere frame sous verrou ; `read()` la renvoie en BGR (OpenCV).

Prerequis (une fois) :
    sudo apt install -y python3-gz-transport13 python3-gz-msgs10
et rendre le venv capable de les importer (cf perception/.venv/.../_gz_syspath.pth).

Topic par defaut = la camera du gimbal d'iris_with_gimbal dans le monde argos_demo.
"""
from __future__ import annotations

import threading

import numpy as np

# Bindings gz : les noms de modules varient selon la version de Gazebo (Harmonic = 13/10).
_IMPORT_ERR = None
try:
    from gz.transport13 import Node
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.double_pb2 import Double
except Exception as e1:  # pragma: no cover - depend de l'install systeme
    try:
        from gz.transport14 import Node
        from gz.msgs11.image_pb2 import Image
        from gz.msgs11.double_pb2 import Double
    except Exception as e2:
        Node = None
        Image = None
        Double = None
        _IMPORT_ERR = f"{e1!r} / {e2!r}"

DEFAULT_TOPIC = ("/world/iris_runway/model/iris_with_gimbal/model/gimbal/"
                 "link/pitch_link/sensor/camera/image")

# Valeurs PixelFormatType (gz.msgs) utiles
_RGB_INT8 = 3
_BGR_INT8 = 6
_L_INT8 = 1


def available() -> bool:
    return Node is not None and Image is not None


def import_error() -> str | None:
    return _IMPORT_ERR


class GzCamera:
    """Abonne-toi au topic image et expose la derniere frame en BGR."""

    def __init__(self, topic: str = DEFAULT_TOPIC):
        if not available():
            raise RuntimeError(f"bindings gz-transport indisponibles: {_IMPORT_ERR}")
        self.topic = topic
        self._frame = None          # np.ndarray HxWx3 BGR
        self._lock = threading.Lock()
        self._count = 0
        self._node = Node()
        if not self._node.subscribe(Image, topic, self._on_image):
            raise RuntimeError(f"echec subscribe sur {topic}")

    def _on_image(self, msg) -> None:
        w, h = msg.width, msg.height
        if w == 0 or h == 0 or not msg.data:
            return
        buf = np.frombuffer(msg.data, dtype=np.uint8)
        pf = msg.pixel_format_type
        try:
            if pf in (_RGB_INT8, 0):           # 0 = UNKNOWN -> on suppose RGB (cas courant)
                img = buf.reshape(h, w, 3)[:, :, ::-1]      # RGB -> BGR
            elif pf == _BGR_INT8:
                img = buf.reshape(h, w, 3)
            elif pf == _L_INT8:
                g = buf.reshape(h, w)
                img = np.stack([g, g, g], axis=-1)
            else:                               # fallback : tente RGB
                img = buf.reshape(h, w, 3)[:, :, ::-1]
        except ValueError:
            return                              # taille incoherente, on ignore la frame
        img = np.ascontiguousarray(img)
        with self._lock:
            self._frame = img
            self._count += 1

    def read(self):
        """Renvoie (ok, frame_bgr). ok=False tant qu'aucune frame n'est arrivee."""
        with self._lock:
            if self._frame is None:
                return False, None
            return True, self._frame.copy()

    @property
    def frames_received(self) -> int:
        with self._lock:
            return self._count


class GzGimbal:
    """Commande le gimbal (pitch/yaw, radians) via les topics JointPositionController.
    Le drone iris Gazebo ne tourne pas en lacet -> c'est le GIMBAL qui slew pour suivre."""
    PITCH = "/gimbal/cmd_pitch"
    YAW = "/gimbal/cmd_yaw"

    def __init__(self):
        if not available() or Double is None:
            raise RuntimeError(f"bindings gz indisponibles: {_IMPORT_ERR}")
        self._node = Node()
        self._pub_pitch = self._node.advertise(self.PITCH, Double)
        self._pub_yaw = self._node.advertise(self.YAW, Double)

    def _pub(self, pub, val):
        msg = Double()
        msg.data = float(val)
        pub.publish(msg)

    def set_pitch(self, rad):
        self._pub(self._pub_pitch, rad)

    def set_yaw(self, rad):
        self._pub(self._pub_yaw, rad)


if __name__ == "__main__":
    # petit smoke-test : grab d'une frame -> PNG (pour voir ce que voit le drone)
    import sys
    import time

    import cv2

    topic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TOPIC
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/gz_frame.png"
    cam = GzCamera(topic)
    print(f"[gz_camera] abonne a {topic}, attente d'une frame ...")
    t0 = time.time()
    while time.time() - t0 < 15:
        ok, frame = cam.read()
        if ok:
            cv2.imwrite(out, frame)
            print(f"[gz_camera] frame {frame.shape} -> {out} ({cam.frames_received} recues)")
            break
        time.sleep(0.2)
    else:
        print("[gz_camera] aucune frame recue (le sim tourne ? camera active ?)")
