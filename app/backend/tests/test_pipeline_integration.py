import subprocess
from pathlib import Path

import pytest

from app.core.models import CaptionStyle, EditSpec, Quality, Reframe, TrackPoint
from app.media.faces import FaceDetector, face_detection_available
from app.media.probe import probe
from app.media.qa import check_clip
from app.media.render import render_clip
from app.pipeline.analyze import analyze_video
from tests.conftest import make_transcript


def _two_scenes(path: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=640x360:r=25:d=2",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


@pytest.mark.integration
def test_analyze_detects_scene_cut(tmp_path: Path):
    src = tmp_path / "scenes.mp4"
    _two_scenes(src)
    sig = analyze_video(src, probe(src), detect_faces=face_detection_available())
    assert len(sig.scene_cuts) == 1 and abs(sig.scene_cuts[0] - 2.0) <= 0.25
    assert sig.face_coverage(0, 4) == 0.0  # padrão de teste não tem rosto


@pytest.mark.integration
@pytest.mark.parametrize("mode", [Reframe.FIT, Reframe.FACE])
def test_render_fit_and_face_track_pass_qa(tmp_path: Path, mode: Reframe):
    src = tmp_path / "scenes.mp4"
    _two_scenes(src)
    info = probe(src)
    track = [TrackPoint(t=0.0, cx=0.1, cy=0.5), TrackPoint(t=4.0, cx=0.9, cy=0.5)] if mode == Reframe.FACE else []
    spec = EditSpec(start=0.5, end=3.5, reframe=mode, track=track, captions=CaptionStyle(enabled=True))
    out = render_clip(src, info, make_transcript("um teste curto de legenda."), spec, tmp_path / "o.mp4", Quality.PREVIEW)
    rep = check_clip(out, (540, 960), 3.0, expect_audio=True)
    assert rep.ok, rep.problems


@pytest.mark.integration
def test_qa_flags_black_and_silent_clip(tmp_path: Path):
    src = tmp_path / "black.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=540x960:r=25:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src),
    ], check=True)
    rep = check_clip(src, (540, 960), 2.0, expect_audio=True)
    assert not rep.ok and any("preto" in p for p in rep.problems)


@pytest.mark.skipif(not face_detection_available(), reason="opencv/YuNet ausente")
def test_face_detector_ignores_blank_frame():
    import numpy as np
    assert FaceDetector().detect(np.zeros((270, 480, 3), np.uint8)) == []


def _talk_video(path: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=40",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=40", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


@pytest.mark.integration
def test_llm_shortfall_is_reported_without_local_curation(tmp_path: Path, monkeypatch):
    import app.pipeline.process as process
    from app.core.config import get_settings
    from app.core.models import ClipCandidate, CurationPrefs

    src = tmp_path / "talk.mp4"
    _talk_video(src)
    text = " ".join(f"O bloco {i} explica o tema {i} em detalhe." for i in range(30))
    monkeypatch.setattr(process, "transcribe", lambda wav, duration, **kw: make_transcript(text, wps=3.0))
    monkeypatch.setattr(process, "curate_llm", lambda t, p, *a: [ClipCandidate(start=0.0, end=8.0, score=90, source="llm")])
    monkeypatch.setenv("GEMINI_VISUAL", "false")  # sem chamadas de rede no teste
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "falsa")
    get_settings.cache_clear()
    try:
        r = process.process_video(src, tmp_path / "out", CurationPrefs(min_duration=5, max_duration=10, max_clips=3),
                                  render_quality=None, detect_faces=False)
    finally:
        get_settings.cache_clear()
    assert r.curation == "llm" and len(r.clips) == 1
    assert r.clips[0].candidate.source == "llm" and r.clips[0].candidate.score == 90
    assert any("só 1 corte" in w for w in r.warnings)


def test_pipeline_requires_llm_key(tmp_path: Path, monkeypatch):
    import app.pipeline.process as process
    from app.core.config import get_settings
    from app.core.models import CurationPrefs
    from app.pipeline.curate_llm import CurationError

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(CurationError, match="não configurada"):
            process.process_video(tmp_path / "nao-existe.mp4", tmp_path / "out", CurationPrefs())
    finally:
        get_settings.cache_clear()


@pytest.mark.integration
def test_llm_failure_fails_the_job(tmp_path: Path, monkeypatch):
    import app.pipeline.process as process
    from app.core.config import get_settings
    from app.core.models import CurationPrefs
    from app.pipeline.curate_llm import CurationError

    src = tmp_path / "talk.mp4"
    _talk_video(src)

    def boom(*a):
        raise CurationError("cota esgotada")

    monkeypatch.setattr(process, "transcribe", lambda wav, duration, **kw: make_transcript("uma frase. outra frase.", wps=3.0))
    monkeypatch.setattr(process, "curate_llm", boom)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "falsa")
    get_settings.cache_clear()
    try:
        with pytest.raises(CurationError, match="cota"):
            process.process_video(src, tmp_path / "out", CurationPrefs(min_duration=1, max_duration=5),
                                  render_quality=None, detect_faces=False)
    finally:
        get_settings.cache_clear()


@pytest.mark.integration
def test_visual_rerank_combines_scores_and_survives_errors(tmp_path: Path, monkeypatch):
    from app.core.models import ClipCandidate
    from app.pipeline import llm_gemini, visual_rerank

    src = tmp_path / "scenes.mp4"
    _two_scenes(src)
    t = make_transcript("um teste curto de legenda. outra frase aqui.")
    cands = [ClipCandidate(start=0.0, end=1.8, score=80, source="llm"), ClipCandidate(start=2.0, end=3.9, score=70, source="llm")]
    seen = {}

    def fake_json(parts, system, schema):
        seen["images"] = sum(1 for p in parts if not isinstance(p, str))
        return schema(items=[{"id": 0, "visual_score": 10, "issues": "tela vermelha parada", "keep": True},
                             {"id": 1, "visual_score": 100, "issues": "", "keep": True}])

    monkeypatch.setattr(llm_gemini, "generate_json", fake_json)
    out = visual_rerank.rerank_visual(src, t, cands, tmp_path)
    assert seen["images"] == 2
    assert [round(c.score, 1) for c in out] == [77.5, 62.5]  # 0,75·texto + 0,25·visual, reordenado
    assert out[0].start == 2.0 and out[0].scores.visual == 100 and "visual IA 100" in out[0].reason

    def boom(*a):
        raise llm_gemini.GeminiError("503")

    monkeypatch.setattr(llm_gemini, "generate_json", boom)
    assert visual_rerank.rerank_visual(src, t, cands, tmp_path) == cands
