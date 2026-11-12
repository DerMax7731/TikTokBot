"""Optional face detection (OpenCV) so vertical crops follow the person on screen."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import cv2
        return hasattr(cv2, "CascadeClassifier")   # removed from the main module in OpenCV 5
    except ImportError:
        return False


def face_center_x(path: str | Path, start: float, length: float, samples: int = 10) -> float | None:
    """Typical horizontal position (0..1) of the largest face in the clip, or None
    if no face shows up in at least a third of the sampled frames."""
    import cv2
    import numpy as np

    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    try:
        centers = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + length * (i + 0.5) / samples) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scale = 640 / max(gray.shape[1], 1)
            if scale < 1:
                gray = cv2.resize(gray, None, fx=scale, fy=scale)
            faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
            if len(faces):
                x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
                centers.append((x + w / 2) / gray.shape[1])
    finally:
        cap.release()
    if len(centers) < max(samples // 3, 1):
        return None
    return float(np.median(centers))
