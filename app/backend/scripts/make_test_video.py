"""Gera um vídeo de teste paisagem (1280x720) a partir de vídeos locais curtos, para exercitar o pipeline inteiro.

Uso: python scripts/make_test_video.py <video_com_fala> <video_extra_sem_audio> <saida.mp4>

Segmentos (≈96 s), escolhidos para cobrir os casos do reframe e da curadoria:
  A  0–20  vídeo com fala à ESQUERDA do quadro (rosto fora do centro)
  B 20–30  padrão de teste + ruído baixo (sem fala, sem rosto; corte de cena)
  C 30–50  vídeo com fala DESLIZANDO da esquerda para a direita (rastreamento)
  D 50–56  tela preta em silêncio
  E 56–76  vídeo com fala à DIREITA
  F 76–96  vídeo extra centralizado, com a fala do primeiro (pessoas pequenas; tela de app)
Limitação: a fala dos segmentos A, C, E e F é a mesma (repetida); serve para testar o pipeline, não a qualidade editorial.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

W, H, FPS = 1280, 720, 30
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]


def ff(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def placed(src: Path, out: Path, x_expr: str, dur: float) -> None:
    ff(["-f", "lavfi", "-i", f"color=c=0x202830:s={W}x{H}:r={FPS}:d={dur}", "-i", str(src),
        "-filter_complex", f"[1:v]scale=-2:{H},setsar=1[fg];[0:v][fg]overlay=x='{x_expr}':y=0:shortest=1[v];"
                           f"[1:a]apad,atrim=0:{dur}[a]",
        "-map", "[v]", "-map", "[a]", "-t", str(dur), *ENC, str(out)])


def main(speech: Path, extra: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        placed(speech, d / "a.mp4", "120", 20)
        ff(["-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}:d=10", "-f", "lavfi", "-i", "anoisesrc=a=0.01:d=10",
            "-t", "10", *ENC, str(d / "b.mp4")])
        placed(speech, d / "c.mp4", "80+t*(W-w-160)/20", 20)
        ff(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d=6", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "6", *ENC, str(d / "d.mp4")])
        placed(speech, d / "e.mp4", f"W-w-120", 20)
        ff(["-f", "lavfi", "-i", f"color=c=0x202830:s={W}x{H}:r={FPS}:d=20", "-i", str(extra), "-i", str(speech),
            "-filter_complex", f"[1:v]scale=-2:{H},setsar=1[fg];[0:v][fg]overlay=x=(W-w)/2:y=0:shortest=1[v];"
                               "[2:a]apad,atrim=0:20[a]",
            "-map", "[v]", "-map", "[a]", "-t", "20", *ENC, str(d / "f.mp4")])
        lst = d / "list.txt"
        lst.write_text("".join(f"file '{d / n}.mp4'\n" for n in "abcdef"))
        ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    print(out)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
