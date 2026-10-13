"""Modelos de domínio compartilhados por pipeline, CLI e (na Etapa 2) API."""

from enum import Enum

from pydantic import BaseModel, Field


class AspectRatio(str, Enum):
    VERTICAL = "9:16"
    SQUARE = "1:1"
    LANDSCAPE = "16:9"
    PORTRAIT = "4:5"

    @property
    def ratio(self) -> tuple[int, int]:
        w, h = self.value.split(":")
        return int(w), int(h)


class Quality(str, Enum):
    PREVIEW = "preview"
    EXPORT = "export"

    @property
    def long_side(self) -> int:
        return 960 if self is Quality.PREVIEW else 1920


class VideoInfo(BaseModel):
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    rotation: int = 0

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation in (90, 270, -90):
            return self.height, self.width
        return self.width, self.height


class Word(BaseModel):
    text: str
    start: float
    end: float
    prob: float = 1.0


class Transcript(BaseModel):
    language: str
    duration: float
    words: list[Word]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text for w in self.words if w.start >= start and w.end <= end)


class ClipScores(BaseModel):
    hook: float = 0.0
    coherence: float = 0.0
    value: float = 0.0
    trend: float = 0.0
    audio: float = 0.0  # energia e ênfase da fala
    visual: float = 0.0  # presença de rosto e ritmo de cenas


class ClipCandidate(BaseModel):
    start: float
    end: float
    title: str = ""
    hook: str = ""
    reason: str = ""
    score: float = 0.0
    scores: ClipScores = Field(default_factory=ClipScores)
    source: str = "llm"

    @property
    def duration(self) -> float:
        return self.end - self.start


class CurationPrefs(BaseModel):
    min_duration: float = 20.0
    max_duration: float = 60.0
    max_clips: int = 8
    prompt: str = ""
    keywords: list[str] = Field(default_factory=list)
    language: str = ""


class CaptionStyle(BaseModel):
    enabled: bool = True
    font: str = ""  # vazio = Settings.caption_font ou procura automática
    size_ratio: float = 0.065  # fração da largura do vídeo
    color: str = "#FFFFFF"
    highlight_color: str = "#FFE135"
    stroke_color: str = "#000000"
    stroke_ratio: float = 0.12  # fração do tamanho da fonte
    position: float = 0.72  # centro vertical (0 = topo, 1 = base)
    uppercase: bool = True
    max_words: int = 3
    max_chars: int = 18


class Reframe(str, Enum):
    AUTO = "auto"  # o estágio REFRAME decide entre face, fit e center
    FACE = "face"  # crop que acompanha o rosto (usa EditSpec.track)
    FIT = "fit"  # quadro inteiro sobre fundo desfocado (sem pessoas, telas, slides)
    CENTER = "center"  # crop central fixo


class TrackPoint(BaseModel):
    """Centro desejado do crop em um instante. `t` em segundos da FONTE (não do corte), para sobreviver a trims;
    `cx`/`cy` normalizados (0..1) na imagem de origem."""

    t: float
    cx: float
    cy: float


class EditSpec(BaseModel):
    """Tudo que define a renderização de um corte. Render = f(fonte, EditSpec)."""

    start: float
    end: float
    aspect: AspectRatio = AspectRatio.VERTICAL
    reframe: Reframe = Reframe.AUTO
    track: list[TrackPoint] = Field(default_factory=list)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    word_overrides: dict[int, str] = Field(default_factory=dict)  # índice global da palavra -> texto editado
