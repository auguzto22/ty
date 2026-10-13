"""Orquestra IMPORT → TRANSCRIBE → ANALYZE → CURATE → REFINE → REFRAME → RENDER → QA para um vídeo.

Cada estágio grava seu artefato em `out_dir` (audio.wav, transcript.json, signals.json, clips.json),
e execuções seguintes reaproveitam o que já existe. O worker da API chama `process_video` e persiste
os eventos de `on_stage` no banco."""

import json
import logging
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.models import ClipCandidate, CurationPrefs, EditSpec, Quality, Transcript, VideoInfo
from app.media.audio import extract_audio, rms_envelope
from app.media.probe import probe
from app.media.qa import QAReport, check_clip
from app.media.render import output_size, render_clip, thumbnail
from app.pipeline.analyze import Signals, analyze_video, load_signals
from app.pipeline.curate_llm import CurationError, curate_llm
from app.pipeline.reframe import plan_reframe
from app.pipeline.visual_rerank import rerank_visual
from app.pipeline.refine import refine
from app.pipeline.transcribe import to_srt, to_txt, transcribe

log = logging.getLogger(__name__)

MAX_VISUAL_POOL = 12
ENVELOPE_WINDOW = 0.5


class Stage(str, Enum):
    QUEUED = "QUEUED"
    IMPORT = "IMPORT"
    TRANSCRIBE = "TRANSCRIBE"
    ANALYZE = "ANALYZE"
    CURATE = "CURATE"
    REFINE = "REFINE"
    REFRAME = "REFRAME"
    RENDER = "RENDER"
    QA = "QA"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


StageCallback = Callable[[Stage, float, str], None]


class ClipResult(BaseModel):
    rank: int
    candidate: ClipCandidate
    spec: EditSpec
    preview: str | None = None
    export: str | None = None
    thumbnail: str | None = None
    qa: QAReport | None = None


class ProcessResult(BaseModel):
    source: str
    info: VideoInfo
    language: str
    curation: str  # "llm", ou "llm+visual" quando houve avaliação visual
    warnings: list[str] = []
    timings: dict[str, float] = {}  # segundos de parede por estágio
    clips: list[ClipResult]


def _noop(stage: Stage, progress: float, msg: str) -> None:
    pass


def load_transcript(out_dir: Path) -> Transcript | None:
    path = out_dir / "transcript.json"
    return Transcript.model_validate_json(path.read_text()) if path.exists() else None


def clip_filename(rank: int, quality: Quality) -> str:
    return f"clip_{rank:02}_{quality.value}.mp4"


def render_and_check(
    source: Path, info: VideoInfo, transcript: Transcript | None, res: ClipResult, out_dir: Path,
    quality: Quality, on_progress: Callable[[float], None] | None = None,
) -> ClipResult:
    """Renderiza um corte na qualidade pedida, gera a miniatura e roda o QA. Atualiza e devolve `res`."""
    clips_dir = out_dir / "clips"
    name = clip_filename(res.rank, quality)
    target = clips_dir / name
    render_clip(source, info, transcript, res.spec, target, quality, on_progress)
    thumb = clips_dir / f"clip_{res.rank:02}.jpg"
    thumbnail(target, min(1.0, (res.spec.end - res.spec.start) / 2), thumb, width=360)
    res.thumbnail = f"clips/{thumb.name}"
    if quality is Quality.EXPORT:
        res.export = f"clips/{name}"
    else:
        res.preview = f"clips/{name}"
    res.qa = check_clip(target, output_size(res.spec, quality), res.spec.end - res.spec.start, info.has_audio)
    return res


def process_video(
    source: Path,
    out_dir: Path,
    prefs: CurationPrefs,
    base_spec: EditSpec | None = None,
    *,
    render_quality: Quality | None = Quality.PREVIEW,
    detect_faces: bool = True,
    reuse: bool = True,
    on_stage: StageCallback = _noop,
) -> ProcessResult:
    """`render_quality=None` só seleciona os cortes (sem renderizar). A curadoria é sempre feita pelo LLM
    configurado; sem chave, ou se o LLM falhar, levanta `CurationError` (não há curadoria local)."""
    settings = get_settings()
    if not settings.llm_enabled:  # falha antes de gastar minutos transcrevendo
        raise CurationError(f"Curadoria por IA não configurada: defina a chave de LLM_PROVIDER={settings.llm_provider}")
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    timings: dict[str, float] = {}
    clock = {"stage": None, "t": time.monotonic()}
    report = on_stage

    def on_stage(stage: Stage, progress: float, msg: str) -> None:
        now = time.monotonic()
        if clock["stage"] is not None:
            timings[clock["stage"]] = round(timings.get(clock["stage"], 0.0) + now - clock["t"], 2)
        clock["stage"], clock["t"] = stage.value, now
        report(stage, progress, msg)

    on_stage(Stage.IMPORT, 0.0, "Lendo metadados")
    info = probe(source)
    if not info.has_audio:
        raise ValueError("O vídeo não tem áudio; não é possível transcrever")
    wav = out_dir / "audio.wav"
    if not (reuse and wav.exists()):
        extract_audio(source, wav)
    on_stage(Stage.IMPORT, 1.0, f"{info.duration:.0f}s, {info.width}x{info.height}")

    transcript = load_transcript(out_dir) if reuse else None
    if transcript is None:
        on_stage(Stage.TRANSCRIBE, 0.0, "Transcrevendo")
        transcript = transcribe(
            wav, info.duration, language=prefs.language,
            on_progress=lambda p: on_stage(Stage.TRANSCRIBE, p, "Transcrevendo"),
        )
        (out_dir / "transcript.json").write_text(transcript.model_dump_json())
        (out_dir / "transcript.srt").write_text(to_srt(transcript))
        (out_dir / "transcript.txt").write_text(to_txt(transcript))
    on_stage(Stage.TRANSCRIBE, 1.0, f"{len(transcript.words)} palavras ({transcript.language})")
    if not transcript.words:
        raise ValueError("Nenhuma fala detectada no vídeo")

    signals_path = out_dir / "signals.json"
    signals: Signals | None = load_signals(signals_path) if reuse else None
    if signals is None or (detect_faces and not signals.faces_enabled):
        on_stage(Stage.ANALYZE, 0.0, "Cenas e rostos")
        signals = analyze_video(source, info, detect_faces=detect_faces,
                                on_progress=lambda p: on_stage(Stage.ANALYZE, p, "Cenas e rostos"))
        signals_path.write_text(signals.model_dump_json())
    on_stage(Stage.ANALYZE, 1.0, f"{len(signals.scene_cuts)} cortes de cena, rostos {'sim' if signals.faces_enabled else 'não'}")

    on_stage(Stage.CURATE, 0.0, "Selecionando trechos com IA")
    candidates = curate_llm(transcript, prefs, rms_envelope(wav, ENVELOPE_WINDOW), ENVELOPE_WINDOW, signals)
    curation = "llm"
    on_stage(Stage.CURATE, 1.0, f"{len(candidates)} candidatos")

    on_stage(Stage.REFINE, 0.0, "Ajustando limites")
    visual = settings.llm_provider == "gemini" and settings.gemini_visual
    # Com a avaliação visual, seleciona mais candidatos para a IA reordenar e só depois corta em max_clips.
    wide = prefs.model_copy(update={"max_clips": min(MAX_VISUAL_POOL, prefs.max_clips * 3)}) if visual else prefs
    chosen = refine(candidates, transcript, wide)
    if not chosen:
        raise CurationError("A IA não sugeriu nenhum corte válido para a duração pedida")
    if len(chosen) < prefs.max_clips:
        warnings.append(f"A IA sugeriu só {len(chosen)} corte(s) válido(s) de {prefs.max_clips} pedidos")
    if visual and len(chosen) > 1:
        on_stage(Stage.REFINE, 0.5, f"Avaliação visual de {len(chosen)} candidatos")
        before = [c.start for c in chosen]
        chosen = rerank_visual(source, transcript, chosen, out_dir)
        if any("visual IA" in c.reason for c in chosen):
            curation += "+visual"
        elif [c.start for c in chosen] == before:
            warnings.append("Avaliação visual por IA indisponível; mantida a ordem do texto")
    chosen = chosen[: prefs.max_clips]
    on_stage(Stage.REFINE, 1.0, f"{len(chosen)} cortes")

    on_stage(Stage.REFRAME, 0.0, "Enquadrando")
    base = base_spec or EditSpec(start=0, end=0)
    results: list[ClipResult] = []
    for rank, cand in enumerate(chosen, 1):
        spec = plan_reframe(signals, info, base.model_copy(update={"start": cand.start, "end": cand.end}))
        results.append(ClipResult(rank=rank, candidate=cand, spec=spec))
    on_stage(Stage.REFRAME, 1.0, ", ".join(r.spec.reframe.value for r in results))

    if render_quality is not None:
        on_stage(Stage.RENDER, 0.0, f"Corte 1/{len(results)}")
        for i, res in enumerate(results):
            def prog(p: float, i: int = i) -> None:
                on_stage(Stage.RENDER, (i + p) / len(results), f"Corte {i + 1}/{len(results)}")

            render_and_check(source, info, transcript, res, out_dir, render_quality, prog)
            if res.qa and not res.qa.ok:
                warnings.append(f"Corte #{res.rank}: QA reprovou ({'; '.join(res.qa.problems)})")
        on_stage(Stage.QA, 1.0, f"{sum(1 for r in results if r.qa and r.qa.ok)}/{len(results)} cortes aprovados")

    result = ProcessResult(
        source=str(source.resolve()), info=info, language=transcript.language,
        curation=curation, warnings=warnings, clips=results,
    )
    on_stage(Stage.COMPLETE, 1.0, f"{len(results)} cortes prontos")
    result.timings = timings
    save_result(result, out_dir)
    return result


def save_result(result: ProcessResult, out_dir: Path) -> None:
    (out_dir / "clips.json").write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
