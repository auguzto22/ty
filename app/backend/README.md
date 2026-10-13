# ClipStudio: backend

Pipeline local de cortes automáticos (M1–M3) com API e worker (M4).

```
IMPORT → TRANSCRIBE (faster-whisper) → ANALYZE (cenas + rostos YuNet) → CURATE (LLM: Gemini ou Claude)
       → REFINE → REFRAME (rosto | fit | centro) → RENDER (ffmpeg + legendas Pillow) → QA
```

## Requisitos
- Python 3.11 ou superior. Testado com 3.14. Também é preciso `ffmpeg`/`ffprobe` no PATH; libass e drawtext não são necessários.
- O modelo YuNet (`app/assets/face_detection_yunet_2023mar.onnx`, ~230 KB) já vem no repositório.
- **Obrigatório:** `GEMINI_API_KEY` (em `.env`, fora do git), ou `ANTHROPIC_API_KEY` com `LLM_PROVIDER=claude`. **Os cortes são sempre escolhidos pela IA; não há curadoria local.** Sem chave, o processamento falha logo no início e a API recusa novos projetos (503). Se o LLM falhar (cota, rede), o job falha e pode ser reprocessado.
  - Teste a chave com `python -m app.cli gemini-check`, que só lista modelos e não gasta tokens. Com `--ping`, faz uma geração mínima.
  - Sobrecarga (503) é repetida com espera e depois cai para `GEMINI_FALLBACK_MODEL`.
  - Se a IA entregar menos cortes válidos que o pedido, o resultado vem só com esses e um aviso em `warnings`.
  - Passo 1: a transcrição vai anotada com pausas, energia, cortes de cena e ausência de rosto.
  - Passo 2 (`GEMINI_VISUAL=true`): o Gemini vê uma grade 3×3 de quadros de cada candidato e a nota visual entra em 25% do score final (`curation` ganha o sufixo `+visual`).

## Instalação
```bash
cd app/backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env   # ajuste WHISPER_MODEL, API_TOKEN etc.
```

## CLI
```bash
# 3 cortes 9:16 exportados em 1080x1920 com legendas (padrão)
.venv/bin/python -m app.cli process video.mp4 --out out/ --min 20 --max 60 --language pt

# Opções: --max-clips N · --quality preview|export|none · --reframe auto|face|fit|center
#         --no-faces · --no-captions · -k palavra-chave · --no-reuse

# Re-renderizar um corte com ajustes (trim, proporção, enquadramento)
.venv/bin/python -m app.cli render --out out/ --rank 1 --start 12.0 --end 40.5 --reframe fit

# Avaliação (M1): Recall@K, cortes no meio de palavra, enquadramento e tempos, sem renderizar e sem LLM
.venv/bin/python -m app.cli eval conjunto.json --work out/eval
```

Saída em `out/`:
- `transcript.json` / `.srt` / `.txt`;
- `signals.json`, com os cortes de cena e os rostos amostrados;
- `clips.json`, com cortes, scores, `EditSpec`, QA e tempos por estágio;
- `clips/clip_NN_export.mp4` (ou `_preview.mp4`) e `clips/clip_NN.jpg`.

Execuções seguintes na mesma pasta reaproveitam o áudio, a transcrição e os sinais.

O formato do conjunto de avaliação está descrito em `app/pipeline/evaluate.py`.

## Contas, cotas e limpeza (Etapa 7)
- **Contas:** `python -m app.cli user-add voce@ex.com [--admin]` cria uma conta, com a senha pedida no terminal. O app entra com e-mail e senha (`POST /auth/login`, que devolve um JWT de 30 dias). Com `ALLOW_SIGNUP=true`, o próprio app pode criar contas.
- **`API_TOKEN`** continua valendo como acesso de administrador: vê todos os projetos e não tem cota. Sem `API_TOKEN` e sem nenhuma conta, a API fica aberta, o que serve só para desenvolvimento local.
- **Isolamento:** cada usuário só enxerga os próprios projetos; um projeto de outro usuário responde 404.
- **Cotas:** `QUOTA_PROJECTS_PER_DAY` e `QUOTA_MINUTES_PER_DAY`, contadas nas últimas 24 h. `GET /me` mostra uso e limites.
- **Limpeza** (desligada por padrão; produção usa 7 dias): o worker roda a política de retenção a cada hora; também dá para rodar `python -m app.cli cleanup [--dry-run]`.
  - `SOURCE_RETENTION_DAYS` apaga o vídeo original e os intermediários. Os cortes continuam disponíveis, mas não dá mais para re-renderizar (a API responde 409).
  - `PROJECT_RETENTION_DAYS` apaga o projeto inteiro.
  - Uploads abandonados são apagados após 1 dia.
- **Banco:** SQLite por padrão; Postgres com `DATABASE_URL=postgresql+psycopg://...` e `pip install -e '.[postgres]'`. Colunas novas são adicionadas automaticamente aos bancos existentes.

## API + worker (M4)
```bash
export API_TOKEN=um-token-seu DATA_DIR=./data
.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000   # documentação em /docs
.venv/bin/python -m app.worker                                    # outro terminal; 1 por máquina
.venv/bin/python scripts/api_smoke.py video.mp4 --url http://127.0.0.1:8000 --token um-token-seu
```

| Método | Rota | Uso |
|---|---|---|
| GET | `/health` | status, LLM ligado ou não, detecção de rosto, cadastro aberto |
| POST | `/auth/register`, `/auth/login` | conta: devolve `{token, user}` |
| GET | `/me` | usuário, uso nas últimas 24 h e cotas |
| POST | `/projects` | cria o projeto com as preferências e devolve a URL de upload |
| PUT | `/uploads/{id}` | `Content-Range: bytes a-b/total` envia uma parte; `bytes */total` consulta. Responde 308 + `Range` ou 201 |
| POST | `/projects/{id}/start` | enfileira o processamento |
| GET | `/projects`, `/projects/{id}` | status, estágio, progresso, posição na fila, tempos |
| DELETE | `/projects/{id}` | apaga o projeto e os arquivos |
| GET | `/projects/{id}/clips` | cortes com URLs versionadas (`?v=N`) |
| GET | `/projects/{id}/transcript.{srt,txt,json}` | transcrição |
| PATCH | `/clips/{id}` | trim, proporção, enquadramento e legendas; gera nova versão e preview |
| POST | `/clips/{id}/render?quality=export` | export em 1080x1920 |
| GET | `/clips/{id}/file?kind=preview\|export\|thumb` | download com suporte a `Range` (206) |

## Testes
```bash
.venv/bin/pytest                      # 41 testes: unitários + integração (ffmpeg, API e worker)
.venv/bin/pytest -m "not integration"
python scripts/make_test_video.py <video_com_fala> <video_extra> teste.mp4   # vídeo paisagem de teste (96 s)
```

## Notas
- O faster-whisper recebe o áudio já decodificado (numpy). O decoder PyAV embutido nele é incompatível com `av>=17`.
- As legendas são PNGs gerados pelo Pillow e compostos com `overlay`. Funcionam em builds do ffmpeg sem libass.
- O enquadramento FACE usa uma "câmera virtual": zona morta, velocidade máxima e salto nos cortes de cena. A trajetória vai ao ffmpeg como expressão `crop` linear por partes. Sem rosto confiável, usa FIT (quadro inteiro sobre fundo desfocado) ou CENTER.
- A arquitetura e as etapas estão em [`../../PLANO_MVP.md`](../../PLANO_MVP.md).
