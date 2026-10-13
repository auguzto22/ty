"""Amostragem de quadros via ffmpeg (rawvideo BGR por pipe), quadro a quadro, sem carregar o vídeo na memória."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.core.models import VideoInfo
from app.media.ffmpeg import FFmpegError


def sample_size(info: VideoInfo, width: int) -> tuple[int, int]:
    src_w, src_h = info.display_size
    w = min(width, src_w) // 2 * 2
    return w, max(2, round(src_h * w / src_w / 2) * 2)


def iter_frames(
    video: Path, info: VideoInfo, fps: float, width: int, start: float = 0.0, end: float | None = None
) -> Iterator[tuple[float, np.ndarray]]:
    """Gera (t na fonte, quadro BGR HxWx3) a `fps` quadros por segundo, entre `start` e `end`."""
    w, h = sample_size(info, width)
    cmd = [get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - start):.3f}"]
    # O ffmpeg aplica a rotação dos metadados automaticamente; o scale usa o tamanho de exibição.
    cmd += ["-an", "-vf", f"fps={fps},scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    frame_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    n = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield start + n / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        code = proc.wait()
    if code not in (0, None) and n == 0:
        raise FFmpegError(f"ffmpeg falhou ao amostrar quadros ({code}): {stderr.strip()[-1000:]}")
