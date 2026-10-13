# ClipStudio: arquitetura

ClipStudio é uma aplicação própria de cortes automáticos de vídeo com legendas. Ela se inspira nas **funcionalidades** que a análise estática do OpusClip identificou (`../../RELATORIO_OPUSCLIP.md`). Não usa APIs privadas (`api.opus.pro`) nem código decompilado: o APK e a pasta `analise_opusclip/` servem apenas de referência e continuam intactos.

## Componentes

```
Android (Kotlin/Compose) ──HTTP/JSON──> API (FastAPI) ──> SQLite
   │ upload retomável (Content-Range)        │ cria jobs
   │ download com Range → MediaStore         ▼
   └─ ExoPlayer (preview)               Worker (fila em tabela)
                                           IMPORT → TRANSCRIBE → CURATE → REFINE → RENDER
                                           ffmpeg · faster-whisper · Claude API · Pillow
```

| Camada | Tecnologia | Status |
|---|---|---|
| Núcleo de mídia + CLI | Python 3.13, ffmpeg, faster-whisper, Pillow, Claude API | **Etapa 1: pronto** |
| API + fila + upload retomável | FastAPI, SQLAlchemy/SQLite, worker em processo separado | Etapa 2 |
| App Android | Kotlin, Compose, Retrofit/OkHttp, WorkManager, Media3 | Etapas 3 a 5 |

## Mapeamento: conceito observado → implementação própria

| Observado no APK (estático) | Aqui |
|---|---|
| Upload retomável no estilo GCS (`Content-Range`, 308 + `Range`, partes de 32 MiB, retentativa exponencial) | Protocolo próprio equivalente em `PUT /uploads/{id}` (Etapa 2); o cliente Android usa WorkManager |
| `CreateProjectRequest` com `curationPref` e `renderPref` | `CurationPrefs` + `EditSpec` (`app/core/models.py`) |
| Estágios PENDING…COMPLETE/STALLED | `Stage` em `app/pipeline/process.py` (IMPORT, TRANSCRIBE, CURATE, REFINE, RENDER, COMPLETE, FAILED) |
| Corte com `score`, `rank` e `judgeResult` (hook/coherence/…) | `ClipCandidate.score` + `ClipScores` (hook, coherence, value, trend) |
| `Screenplay → … → Word` e transcrição TXT/SRT | `Transcript.words` com tempos por palavra; `transcript.srt` / `.txt` |
| `TemplatePreferences` (fonte, cor, highlight) | `CaptionStyle` |
| Render `VIDEO_PREVIEW` / `VIDEO_FILE` | `Quality.PREVIEW` (540x960) / `Quality.EXPORT` (1080x1920) |
| Cache de download versionado (`.partial`/`.complete`, parâmetro `v`) | `Clip.version` incrementa a cada edição (Etapa 2/4) |
| MediaStore `DCIM/OpusClip` | MediaStore `Movies/ClipStudio` (Etapa 4) |

O que a análise **não** revelou, porque roda nos servidores deles (seleção dos cortes, ASR, reframe, codecs), foi projetado aqui do zero:

- **ASR:** faster-whisper na CPU (int8), com timestamps por palavra e VAD.
- **Curadoria:**
  - O LLM (Gemini por padrão, Claude opcional) recebe a transcrição numerada por frase e devolve, em JSON validado por schema Pydantic, as frases de início e fim, o título, o gancho e os scores.
  - **Só IA:** não há fallback heurístico. Sem chave ou com falha do LLM, o processamento falha.
- **Refine:** encaixa os cortes em fronteiras de frase, aplica as durações mínima e máxima e limita a sobreposição a 30%.
- **Legendas:**
  - O ffmpeg local **não tem libass/drawtext**. Por isso o Pillow gera um PNG por estado (página + palavra ativa), o demuxer `concat` define quanto cada um fica na tela e um único `overlay` compõe o resultado.
  - Isso funciona com qualquer build do ffmpeg.
- **Render:** é uma função determinística de `(fonte, EditSpec)`. O crop é central no MVP; o reframe por rosto entra na Etapa 6. A saída é H.264/AAC com `+faststart`.

## Etapas

1. **Núcleo de mídia + CLI.** Concluída.
2. API FastAPI + SQLite + worker + upload retomável + download com Range.
3. Android: setup, projetos, upload (WorkManager) e progresso.
4. Android: lista de cortes, preview, export, MediaStore e compartilhamento.
5. Editor: trim, texto e estilo das legendas, proporção; re-render por versão.
6. Qualidade: reframe por rosto, auto hook, presets, limpeza de armazenamento.
7. Produção: contas (JWT), Postgres, cotas e limpeza (sem Docker).
