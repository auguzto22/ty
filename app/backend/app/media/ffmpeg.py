import subprocess
from collections.abc import Callable, Sequence

from app.core.config import get_settings


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(
    args: Sequence[str],
    *,
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Executa o ffmpeg; com `duration` e `on_progress`, reporta a fração concluída (0..1)."""
    cmd = [get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if on_progress and duration:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += list(args)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if on_progress and duration:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                on_progress(min(1.0, int(value) / 1e6 / duration))
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg falhou ({proc.returncode}): {stderr.strip()[-2000:]}")
    if on_progress:
        on_progress(1.0)
