import wave
from pathlib import Path

import numpy as np

from app.media.ffmpeg import run_ffmpeg

SAMPLE_RATE = 16_000


def extract_audio(video: Path, out_wav: Path) -> Path:
    """Extrai áudio mono PCM 16 kHz — formato esperado pelo Whisper e pela análise de energia."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out_wav)])
    return out_wav


def load_wav(wav_path: Path) -> tuple[np.ndarray, int]:
    """Lê o WAV PCM 16-bit mono gerado por `extract_audio` como float32 em [-1, 1]."""
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, rate


def rms_envelope(wav_path: Path, window: float = 0.5) -> np.ndarray:
    """Energia RMS normalizada (0..1) por janela de `window` segundos."""
    samples, rate = load_wav(wav_path)
    hop = max(1, int(rate * window))
    n = len(samples) // hop
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    rms = np.sqrt(np.mean(samples[: n * hop].reshape(n, hop) ** 2, axis=1))
    peak = float(np.percentile(rms, 98)) or 1.0
    return np.clip(rms / peak, 0.0, 1.0)
