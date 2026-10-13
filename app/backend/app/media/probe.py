import json
import subprocess
from pathlib import Path

from app.core.config import get_settings
from app.core.models import VideoInfo
from app.media.ffmpeg import FFmpegError


def probe(path: Path) -> VideoInfo:
    cmd = [
        get_settings().ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe falhou: {res.stderr.strip()}")
    data = json.loads(res.stdout)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path} não tem trilha de vídeo")
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])

    num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 30.0

    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"]) % 360
    if "rotate" in video.get("tags", {}):
        rotation = int(video["tags"]["rotate"]) % 360

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return VideoInfo(
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps or 30.0,
        has_audio=has_audio,
        rotation=rotation,
    )
