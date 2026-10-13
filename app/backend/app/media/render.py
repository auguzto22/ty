import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from app.core.models import EditSpec, Quality, Reframe, Transcript, VideoInfo
from app.media.captions import build_caption_track, clip_words
from app.media.ffmpeg import run_ffmpeg
from app.pipeline.reframe import crop_positions

LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"  # alvo usual de redes sociais; passada única (modo dinâmico)
MAX_TRACK_POINTS = 240

ENCODE = {
    Quality.PREVIEW: ["-preset", "veryfast", "-crf", "26"],
    Quality.EXPORT: ["-preset", "medium", "-crf", "20"],
}


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def output_size(spec: EditSpec, quality: Quality) -> tuple[int, int]:
    rw, rh = spec.aspect.ratio
    long = quality.long_side
    return (_even(long * rw / rh), long) if rh >= rw else (long, _even(long * rh / rw))


def crop_box(info: VideoInfo, spec: EditSpec) -> tuple[int, int, int, int]:
    """Crop central na proporção alvo: (largura, altura, x, y)."""
    src_w, src_h = info.display_size
    rw, rh = spec.aspect.ratio
    target = rw / rh
    if src_w / src_h > target:
        cw, ch = _even(src_h * target), _even(src_h)
    else:
        cw, ch = _even(src_w), _even(src_w / target)
    cw, ch = min(cw, src_w - src_w % 2), min(ch, src_h - src_h % 2)
    return cw, ch, (src_w - cw) // 2, (src_h - ch) // 2


def _piecewise(points: list[tuple[float, int]]) -> str:
    """Expressão do ffmpeg, linear por partes em `t`, sem aninhamento (soma de janelas)."""
    if len(points) == 1:
        return str(points[0][1])
    terms = [f"lt(t,{points[0][0]:.3f})*{points[0][1]}"]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t1 - t0 <= 1e-6:
            continue
        slope = (v1 - v0) / (t1 - t0)
        terms.append(f"gte(t,{t0:.3f})*lt(t,{t1:.3f})*({v0}+{slope:.4f}*(t-{t0:.3f}))")
    terms.append(f"gte(t,{points[-1][0]:.3f})*{points[-1][1]}")
    return "+".join(terms)


def _video_chain(info: VideoInfo, spec: EditSpec, out_w: int, out_h: int, fps: float) -> str:
    """Filtro do vídeo base (sem legendas) terminando em [base]."""
    tail = f"setsar=1,fps={fps:.3f}[base]"
    if spec.reframe == Reframe.FIT:
        bw, bh = _even(out_w / 8), _even(out_h / 8)
        return (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=6:2,"
            f"scale={out_w}:{out_h}:flags=bilinear[bg];"
            f"[fgsrc]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}"
        )
    cw, ch, cx, cy = crop_box(info, spec)
    if spec.reframe == Reframe.FACE and spec.track:
        pts = [p for p in crop_positions(spec, info, cw, ch) if -1.0 <= p[0] <= spec.end - spec.start + 1.0]
        if len(pts) > MAX_TRACK_POINTS:
            step = len(pts) / MAX_TRACK_POINTS
            pts = [pts[int(i * step)] for i in range(MAX_TRACK_POINTS)] + [pts[-1]]
        if pts:
            x = _piecewise([(t, x) for t, x, _ in pts])
            y = _piecewise([(t, y) for t, _, y in pts])
            return f"[0:v]crop=w={cw}:h={ch}:x='{x}':y='{y}',scale={out_w}:{out_h}:flags=lanczos,{tail}"
    return f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{out_h}:flags=lanczos,{tail}"


def render_clip(
    source: Path,
    info: VideoInfo,
    transcript: Transcript | None,
    spec: EditSpec,
    out_path: Path,
    quality: Quality = Quality.PREVIEW,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    duration = spec.end - spec.start
    if duration <= 0:
        raise ValueError("Corte com duração inválida")
    out_w, out_h = output_size(spec, quality)
    fps = min(info.fps, 30.0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="caps_", dir=out_path.parent))
    try:
        args = ["-ss", f"{spec.start:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
        filters = [_video_chain(info, spec, out_w, out_h, fps)]
        last = "base"

        words = clip_words(transcript, spec.start, spec.end, spec.word_overrides) if transcript else []
        if spec.captions.enabled and words:
            concat, band_h = build_caption_track(words, duration, spec.captions, out_w, work)
            y = int(out_h * spec.captions.position - band_h / 2)
            y = max(0, min(out_h - band_h, y))
            args += ["-f", "concat", "-safe", "0", "-i", str(concat)]
            filters.append(f"[1:v]format=rgba[cap];[base][cap]overlay=0:{y}:eof_action=pass:format=auto[v]")
            last = "v"

        if info.has_audio:
            filters.append(f"[0:a:0]{LOUDNORM},aresample=48000[a]")
        args += ["-filter_complex", ";".join(filters), "-map", f"[{last}]"]
        if info.has_audio:
            args += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        args += [
            "-c:v", "libx264", *ENCODE[quality], "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-t", f"{duration:.3f}", str(out_path),
        ]
        run_ffmpeg(args, duration=duration, on_progress=on_progress)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_path


def thumbnail(source: Path, at: float, out_path: Path, width: int = 480) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-ss", f"{at:.3f}", "-i", str(source), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out_path)])
    return out_path
