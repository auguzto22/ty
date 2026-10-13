"""Detecção de rostos local com YuNet (OpenCV DNN, modelo ONNX de ~230 KB em app/assets)."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "face_detection_yunet_2023mar.onnx"
MIN_SCORE = 0.6
MIN_FACE = 0.035  # largura mínima do rosto como fração da menor dimensão do quadro (ignora miniaturas)


@dataclass(frozen=True)
class Face:
    """Caixa normalizada (0..1) no quadro amostrado."""

    cx: float
    cy: float
    w: float
    h: float
    score: float

    @property
    def area(self) -> float:
        return self.w * self.h


class FaceDetector:
    def __init__(self, model_path: Path = MODEL_PATH):
        import cv2  # import tardio: só quem usa rosto paga o custo

        if not model_path.is_file():
            raise FileNotFoundError(f"Modelo YuNet não encontrado em {model_path}")
        self._cv2 = cv2
        self._model_path = str(model_path)
        self._size: tuple[int, int] | None = None
        self._det = None

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._det = self._cv2.FaceDetectorYN.create(self._model_path, "", (w, h), MIN_SCORE, 0.3, 50)
            self._size = (w, h)
        _, found = self._det.detect(frame)
        if found is None:
            return []
        min_px = MIN_FACE * min(w, h)
        faces = []
        for x, y, fw, fh, *_, score in found:
            if fw < min_px or score < MIN_SCORE:
                continue
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(w), x + fw), min(float(h), y + fh)
            faces.append(Face(
                cx=round((x0 + x1) / 2 / w, 4), cy=round((y0 + y1) / 2 / h, 4),
                w=round((x1 - x0) / w, 4), h=round((y1 - y0) / h, 4), score=round(float(score), 3),
            ))
        return faces


@lru_cache
def face_detection_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return MODEL_PATH.is_file()
