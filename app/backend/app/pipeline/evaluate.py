"""M1: avaliação da seleção de cortes contra um conjunto de vídeos com momentos marcados à mão.

Formato do conjunto (JSON):
{
  "videos": [
    {"path": "videos/aula.mp4", "language": "pt", "min": 20, "max": 60,
     "moments": [[123.0, 160.5], [400, 452]], "notes": "opcional"}
  ]
}
Caminhos relativos são resolvidos a partir da pasta do JSON. `moments` são os trechos que você postaria.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.models import CurationPrefs, EditSpec, Reframe, Transcript
from app.pipeline.process import ProcessResult, process_video

HIT_OVERLAP = 0.5  # fração do momento marcado coberta por um corte para contar como acerto
WORD_MARGIN = 0.05  # s; início/fim a menos disso dentro de uma palavra não conta como "corte no meio"


class EvalVideo(BaseModel):
    path: str
    language: str = ""
    min: float = 20.0
    max: float = 60.0
    moments: list[tuple[float, float]] = Field(default_factory=list)
    notes: str = ""


class EvalSet(BaseModel):
    videos: list[EvalVideo]


class VideoMetrics(BaseModel):
    path: str
    clips: int
    recall_at_3: float | None
    recall_at_5: float | None
    precision_at_3: float | None
    cuts_inside_word: float  # fração de inícios/fins que caem no meio de uma palavra
    ends_with_punctuation: float
    reframe_modes: dict[str, int]
    face_jitter: float | None  # deslocamento médio do crop (larguras da fonte por segundo) nos cortes FACE
    timings: dict[str, float]


def _covered(moment: tuple[float, float], clip: tuple[float, float]) -> float:
    inter = max(0.0, min(moment[1], clip[1]) - max(moment[0], clip[0]))
    return inter / max(1e-6, moment[1] - moment[0])


def _recall(moments: list[tuple[float, float]], clips: list[tuple[float, float]], k: int) -> float | None:
    if not moments:
        return None
    top = clips[:k]
    return sum(1 for m in moments if any(_covered(m, c) >= HIT_OVERLAP for c in top)) / len(moments)


def _precision(moments: list[tuple[float, float]], clips: list[tuple[float, float]], k: int) -> float | None:
    top = clips[:k]
    if not moments or not top:
        return None
    return sum(1 for c in top if any(_covered(c, m) >= HIT_OVERLAP or _covered(m, c) >= HIT_OVERLAP for m in moments)) / len(top)


def _inside_word(t: float, transcript: Transcript) -> bool:
    return any(w.start + WORD_MARGIN < t < w.end - WORD_MARGIN for w in transcript.words)


def _jitter(result: ProcessResult) -> float | None:
    speeds = []
    for c in result.clips:
        if c.spec.reframe != Reframe.FACE or len(c.spec.track) < 2:
            continue
        pts = c.spec.track
        moved = sum(abs(b.cx - a.cx) for a, b in zip(pts, pts[1:]) if b.t - a.t > 0.05)  # ignora saltos de cena
        speeds.append(moved / max(1e-6, c.spec.end - c.spec.start))
    return round(sum(speeds) / len(speeds), 4) if speeds else None


def metrics_for(video: EvalVideo, result: ProcessResult, transcript: Transcript) -> VideoMetrics:
    clips = [(c.candidate.start, c.candidate.end) for c in result.clips]
    bounds = [t for c in clips for t in c]
    modes: dict[str, int] = {}
    for c in result.clips:
        modes[c.spec.reframe.value] = modes.get(c.spec.reframe.value, 0) + 1
    ends_punct = [
        any(w.text.endswith((".", "!", "?", "…")) for w in transcript.words if c[1] - 1.0 <= w.end <= c[1]) for c in clips
    ]
    return VideoMetrics(
        path=video.path, clips=len(clips),
        recall_at_3=_recall(video.moments, clips, 3), recall_at_5=_recall(video.moments, clips, 5),
        precision_at_3=_precision(video.moments, clips, 3),
        cuts_inside_word=round(sum(_inside_word(t, transcript) for t in bounds) / len(bounds), 3) if bounds else 0.0,
        ends_with_punctuation=round(sum(ends_punct) / len(ends_punct), 3) if ends_punct else 0.0,
        reframe_modes=modes, face_jitter=_jitter(result), timings=result.timings,
    )


def run_eval(dataset: Path, work_dir: Path, *, detect_faces: bool = True, on_video=None) -> list[VideoMetrics]:
    """Roda o pipeline sem renderizar (só seleção por IA + enquadramento) e mede cada vídeo."""
    data = EvalSet.model_validate_json(dataset.read_text())
    out: list[VideoMetrics] = []
    for v in data.videos:
        src = Path(v.path) if Path(v.path).is_absolute() else (dataset.parent / v.path)
        vid_dir = work_dir / src.stem
        if on_video:
            on_video(v)
        prefs = CurationPrefs(min_duration=v.min, max_duration=v.max, max_clips=5, language=v.language)
        result = process_video(src, vid_dir, prefs, EditSpec(start=0, end=0),
                               render_quality=None, detect_faces=detect_faces)
        transcript = Transcript.model_validate_json((vid_dir / "transcript.json").read_text())
        out.append(metrics_for(v, result, transcript))
    (work_dir / "eval_report.json").write_text(
        json.dumps([m.model_dump() for m in out], ensure_ascii=False, indent=2))
    return out
