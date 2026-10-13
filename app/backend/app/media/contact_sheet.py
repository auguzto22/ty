"""Folha de contato: grade NxN de quadros igualmente espaçados de um trecho, com o tempo de cada quadro."""

from pathlib import Path

from app.media.ffmpeg import run_ffmpeg


def contact_sheet(source: Path, start: float, end: float, out: Path, grid: int = 3, cell_width: int = 256) -> Path:
    """Gera um JPEG com grid×grid quadros de [start, end). Uma única passada do ffmpeg."""
    n = grid * grid
    dur = max(0.5, end - start)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(source), "-an",
        "-vf", f"fps={n / dur:.5f},scale={cell_width}:-2,tile={grid}x{grid}:padding=4:color=black",
        "-frames:v", "1", "-q:v", "5", str(out),
    ])
    return out
