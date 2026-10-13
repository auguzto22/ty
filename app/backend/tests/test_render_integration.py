import json
import subprocess
from pathlib import Path

import pytest

from app.core.models import EditSpec, Quality
from app.media.probe import probe
from app.media.render import render_clip
from tests.conftest import make_transcript


@pytest.mark.integration
def test_render_vertical_with_captions(tmp_path: Path):
    src = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src),
    ], check=True)
    t = make_transcript("primeira frase de teste. segunda frase com mais palavras aqui.")
    out = render_clip(src, probe(src), t, EditSpec(start=1.0, end=6.0), tmp_path / "out.mp4", Quality.PREVIEW)

    meta = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)
    v = next(s for s in meta["streams"] if s["codec_type"] == "video")
    a = next(s for s in meta["streams"] if s["codec_type"] == "audio")
    assert (v["codec_name"], v["width"], v["height"]) == ("h264", 540, 960)
    assert a["codec_name"] == "aac"
    assert abs(float(meta["format"]["duration"]) - 5.0) < 0.2
