"""Legendas queimadas sem libass: cada estado (página + palavra ativa) vira um PNG transparente;
um arquivo do demuxer `concat` define quanto tempo cada PNG fica na tela, e o ffmpeg aplica tudo com um único `overlay`."""

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.core.models import CaptionStyle, Transcript

FONT_CANDIDATES = [
    "/usr/share/fonts/noto/NotoSans-Black.ttf",
    "/run/host/fonts/noto/NotoSans-Black.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
PAGE_BREAK_GAP = 0.6  # pausa (s) que força nova página
MIN_FRAME = 0.04  # menor duração de um estado (s) — evita entradas de 0 ms no concat


@dataclass
class CapWord:
    text: str
    start: float  # relativo ao início do corte
    end: float


@dataclass
class Page:
    words: list[CapWord]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


@lru_cache
def resolve_font(preferred: str = "") -> str:
    for path in [preferred, get_settings().caption_font, *FONT_CANDIDATES]:
        if path and Path(path).is_file():
            return path
    try:
        found = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True).stdout.strip()
        if found and Path(found).is_file():
            return found
    except FileNotFoundError:
        pass
    raise FileNotFoundError("Nenhuma fonte TTF encontrada; defina CAPTION_FONT no .env")


def clip_words(transcript: Transcript, start: float, end: float, overrides: dict[int, str] | None = None) -> list[CapWord]:
    overrides = overrides or {}
    out: list[CapWord] = []
    for i, w in enumerate(transcript.words):
        if w.end <= start or w.start >= end:
            continue
        text = overrides.get(i, w.text).strip()
        if text:
            out.append(CapWord(text=text, start=max(0.0, w.start - start), end=min(end, w.end) - start))
    return out


def paginate(words: list[CapWord], style: CaptionStyle) -> list[Page]:
    pages: list[Page] = []
    cur: list[CapWord] = []
    for w in words:
        too_long = len(" ".join(x.text for x in cur + [w])) > style.max_chars
        gap = cur and (w.start - cur[-1].end) > PAGE_BREAK_GAP
        if cur and (len(cur) >= style.max_words or too_long or gap):
            pages.append(Page(cur))
            cur = []
        cur.append(w)
        if w.text.endswith((".", "?", "!")):
            pages.append(Page(cur))
            cur = []
    if cur:
        pages.append(Page(cur))
    return pages


def _lines(words: list[str], font: ImageFont.FreeTypeFont, max_width: float) -> list[list[int]]:
    """Quebra em linhas por largura; retorna índices das palavras de cada linha."""
    lines: list[list[int]] = [[]]
    for i, word in enumerate(words):
        candidate = " ".join(words[j] for j in lines[-1] + [i])
        if lines[-1] and font.getlength(candidate) > max_width:
            lines.append([])
        lines[-1].append(i)
    return lines


def render_page(page: Page, active: int | None, style: CaptionStyle, width: int, band_h: int, font_path: str) -> Image.Image:
    size = max(12, int(width * style.size_ratio))
    font = ImageFont.truetype(font_path, size)
    stroke = max(1, int(size * style.stroke_ratio))
    texts = [w.text.upper() if style.uppercase else w.text for w in page.words]

    img = Image.new("RGBA", (width, band_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lines = _lines(texts, font, width * 0.88)
    line_h = int(size * 1.25)
    y = (band_h - line_h * len(lines)) // 2
    space = font.getlength(" ")
    for line in lines:
        total = sum(font.getlength(texts[i]) for i in line) + space * (len(line) - 1)
        x = (width - total) / 2
        for i in line:
            color = style.highlight_color if i == active else style.color
            draw.text((x, y), texts[i], font=font, fill=color, stroke_width=stroke, stroke_fill=style.stroke_color)
            x += font.getlength(texts[i]) + space
        y += line_h
    return img


def build_caption_track(
    words: list[CapWord], duration: float, style: CaptionStyle, width: int, out_dir: Path
) -> tuple[Path, int]:
    """Gera PNGs + concat.txt. Retorna (caminho do concat, altura da faixa)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font_path = resolve_font(style.font)
    band_h = int(width * style.size_ratio * 1.25 * 3 + width * 0.05)  # cabe até 3 linhas
    blank = out_dir / "blank.png"
    Image.new("RGBA", (width, band_h), (0, 0, 0, 0)).save(blank)

    timeline: list[tuple[Path, float]] = []
    cursor = 0.0

    def push(path: Path, until: float) -> None:
        nonlocal cursor
        until = min(until, duration)
        if until - cursor >= MIN_FRAME:
            timeline.append((path, until - cursor))
            cursor = until

    n = 0
    pages = paginate(words, style)
    for p_idx, page in enumerate(pages):
        push(blank, page.start)
        page_end = pages[p_idx + 1].start if p_idx + 1 < len(pages) else duration
        page_end = min(page_end, page.end + PAGE_BREAK_GAP)
        for k, w in enumerate(page.words):
            img_path = out_dir / f"cap_{n:05}.png"
            n += 1
            render_page(page, k, style, width, band_h, font_path).save(img_path, compress_level=1)
            nxt = page.words[k + 1].start if k + 1 < len(page.words) else page_end
            push(img_path, max(nxt, cursor + MIN_FRAME))
    push(blank, duration)
    if not timeline:
        timeline.append((blank, max(duration, MIN_FRAME)))

    concat = out_dir / "concat.txt"
    lines = ["ffconcat version 1.0"]
    for path, dur in timeline:
        lines += [f"file '{_escape(path)}'", f"duration {dur:.3f}"]
    lines.append(f"file '{_escape(timeline[-1][0])}'")  # o concat ignora a duração da última entrada
    concat.write_text("\n".join(lines) + "\n")
    return concat, band_h


def _escape(path: Path) -> str:
    return str(path.resolve()).replace("'", r"'\''")
