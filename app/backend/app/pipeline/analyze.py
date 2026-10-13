"""ANALYZE: sinais locais e baratos para a curadoria e o reframe, numa única decodificação do vídeo.

- cortes de cena: diferença de histograma HSV + diferença média de luminância entre quadros consecutivos;
- rostos: YuNet a cada `FACE_EVERY` quadros amostrados;
- energia do áudio: envelope RMS do WAV (calculado à parte, é instantâneo).
"""

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from app.core.models import VideoInfo
from app.media.faces import Face, FaceDetector, face_detection_available
from app.media.frames import iter_frames

log = logging.getLogger(__name__)

SAMPLE_FPS = 5.0
SAMPLE_WIDTH = 480
FACE_EVERY = 2  # rostos a SAMPLE_FPS / FACE_EVERY = 2,5 fps
SCENE_HIST = 0.45  # distância de Bhattacharyya mínima para corte de cena
SCENE_LUMA = 0.12  # diferença média de luminância (0..1) mínima, junto com a de histograma
MIN_SCENE_GAP = 0.6  # s entre cortes (evita flashes contados várias vezes)


class FaceSample(BaseModel):
    t: float
    faces: list[tuple[float, float, float, float, float]] = Field(default_factory=list)  # (cx, cy, w, h, score)

    def as_faces(self) -> list[Face]:
        return [Face(*f) for f in self.faces]


class Signals(BaseModel):
    version: int = 1
    sample_fps: float = SAMPLE_FPS
    scene_cuts: list[float] = Field(default_factory=list)
    face_samples: list[FaceSample] = Field(default_factory=list)
    faces_enabled: bool = False

    def scene_cuts_between(self, start: float, end: float) -> list[float]:
        return [c for c in self.scene_cuts if start < c < end]

    def face_coverage(self, start: float, end: float) -> float:
        """Fração das amostras em [start, end) com pelo menos um rosto."""
        window = [s for s in self.face_samples if start <= s.t < end]
        if not window:
            return 0.0
        return sum(1 for s in window if s.faces) / len(window)


def _hist(cv2, frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [32, 16], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def analyze_video(
    video: Path, info: VideoInfo, *, detect_faces: bool = True, on_progress: Callable[[float], None] | None = None
) -> Signals:
    import cv2

    use_faces = detect_faces and face_detection_available()
    if detect_faces and not use_faces:
        log.warning("Detecção de rosto indisponível (opencv ou modelo YuNet ausente); reframe usará crop central/fit")
    detector = FaceDetector() if use_faces else None

    cuts: list[float] = []
    samples: list[FaceSample] = []
    prev_hist = prev_luma = None
    last_report = 0.0
    for i, (t, frame) in enumerate(iter_frames(video, info, SAMPLE_FPS, SAMPLE_WIDTH)):
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        luma = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        hist = _hist(cv2, small)
        if prev_hist is not None:
            d_hist = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
            d_luma = float(np.abs(luma - prev_luma).mean())
            if d_hist > SCENE_HIST and d_luma > SCENE_LUMA and (not cuts or t - cuts[-1] >= MIN_SCENE_GAP):
                cuts.append(round(t, 2))
        prev_hist, prev_luma = hist, luma

        if detector is not None and i % FACE_EVERY == 0:
            faces = detector.detect(frame)
            samples.append(FaceSample(t=round(t, 2), faces=[(f.cx, f.cy, f.w, f.h, f.score) for f in faces]))

        if on_progress and info.duration and t - last_report >= 5:
            last_report = t
            on_progress(min(0.99, t / info.duration))
    if on_progress:
        on_progress(1.0)
    return Signals(scene_cuts=cuts, face_samples=samples, faces_enabled=detector is not None)


def load_signals(path: Path) -> Signals | None:
    if not path.exists():
        return None
    try:
        s = Signals.model_validate_json(path.read_text())
    except ValueError:
        return None
    return s if s.version == Signals().version else None
