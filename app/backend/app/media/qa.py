"""QA automático do MP4 renderizado: formato, duração, quadros pretos e áudio audível."""

import json
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import get_settings

MAX_BLACK_FRACTION = 0.25
MIN_MEAN_VOLUME_DB = -45.0


class QAReport(BaseModel):
    ok: bool
    width: int = 0
    height: int = 0
    duration: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    black_fraction: float = 0.0
    mean_volume_db: float | None = None
    problems: list[str] = Field(default_factory=list)


def check_clip(path: Path, expected_size: tuple[int, int], expected_duration: float, expect_audio: bool) -> QAReport:
    s = get_settings()
    meta = json.loads(subprocess.run(
        [s.ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)
    v = next((x for x in meta["streams"] if x["codec_type"] == "video"), {})
    a = next((x for x in meta["streams"] if x["codec_type"] == "audio"), {})
    rep = QAReport(
        ok=True, width=int(v.get("width", 0)), height=int(v.get("height", 0)),
        duration=round(float(meta["format"].get("duration", 0)), 3),
        video_codec=v.get("codec_name", ""), audio_codec=a.get("codec_name", ""),
    )

    # Uma passada: blackdetect no vídeo e volumedetect no áudio.
    args = [s.ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(path), "-vf", "blackdetect=d=0.3:pix_th=0.10"]
    args += ["-af", "volumedetect"] if a else ["-an"]
    log = subprocess.run(args + ["-f", "null", "-"], capture_output=True, text=True).stderr
    black = sum(float(m) for m in re.findall(r"black_duration:([\d.]+)", log))
    rep.black_fraction = round(black / rep.duration, 3) if rep.duration else 0.0
    m = re.search(r"mean_volume: (-?[\d.]+) dB", log)
    rep.mean_volume_db = float(m.group(1)) if m else None

    if rep.video_codec != "h264":
        rep.problems.append(f"codec de vídeo {rep.video_codec or 'ausente'}")
    if (rep.width, rep.height) != expected_size:
        rep.problems.append(f"resolução {rep.width}x{rep.height} ≠ {expected_size[0]}x{expected_size[1]}")
    if abs(rep.duration - expected_duration) > 0.35:
        rep.problems.append(f"duração {rep.duration:.2f}s ≠ {expected_duration:.2f}s")
    if expect_audio and rep.audio_codec != "aac":
        rep.problems.append("sem trilha AAC")
    if rep.black_fraction > MAX_BLACK_FRACTION:
        rep.problems.append(f"{rep.black_fraction:.0%} do corte em quadro preto")
    if expect_audio and rep.mean_volume_db is not None and rep.mean_volume_db < MIN_MEAN_VOLUME_DB:
        rep.problems.append(f"áudio quase mudo ({rep.mean_volume_db:.1f} dB)")
    rep.ok = not rep.problems
    return rep
