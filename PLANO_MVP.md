# ClipStudio: plano de arquitetura e MVP

> Documento de arquitetura. Ele se baseia no relatório estático `RELATORIO_OPUSCLIP.md` e no código já entregue em `app/` (Etapa 1).
> Data: 2026-10-12. Status: **aguardando aprovação** das decisões da seção 10.
> **Revisão 6 (2026-10-13):** a curadoria passa a ser **100% por IA**: a heurística local foi removida (sem chave ou com falha do LLM, o processamento falha). Docker, Caddy e `app/deploy/` foram removidos; execução só local. Menções a fallback heurístico e Docker abaixo são históricas.
> **Revisão 5 (2026-10-13):** IA multimodal (seção 4.1, passos 1 e 2) testada com o Gemini real. Etapa 7 parcial: contas com JWT, isolamento por usuário, cotas, limpeza, Postgres configurável, Docker e Caddy (estes dois sem teste). Ver `app/deploy/README.md`.
> **Revisão 4 (2026-10-12):** app Android (M5–M7) implementado. Falta validar num aparelho físico.
> **Revisão 3 (2026-10-12):** M1–M4 implementados com processamento 100% local (ver seção 8). O Gemini fica desligado até a chave ser configurada.
> **Revisão 2 (2026-10-12):** a IA principal passa a ser o **Gemini (Google AI Studio, SDK `google-genai`)**. O Claude continua como provedor alternativo opcional. Nova seção 4.1: análise multimodal.

## 0. Ponto de partida

**O que a análise do APK nos ensinou.** Ela mostra só o que o *cliente* faz:

- upload retomável;
- parâmetros de curadoria e de render;
- estágios do job;
- lista de cortes com score e rank;
- transcrição por palavra;
- download com Range e gravação no MediaStore.

**O que a análise não ensinou.** A seleção dos cortes, o ASR, o reframe, as legendas e o render rodam nos servidores deles e **não foram observados**. Por isso o motor de cortes abaixo é um projeto independente. Não usa `api.opus.pro`, código decompilado nem tenta imitar algoritmos privados. Os contratos do APK servem só como lista de *funcionalidades* esperadas pelo usuário.

**O que já existe (Etapa 1, `app/backend`).**

| Componente | Situação |
|---|---|
| Transcrição local (faster-whisper int8, timestamps por palavra) | pronto |
| Curadoria por LLM (Gemini por padrão, Claude opcional; JSON validado) e fallback heurístico | pronto |
| Refine (fronteira de frase, duração mín./máx., sobreposição ≤30%) | pronto |
| Legendas queimadas via Pillow e overlay (ffmpeg sem libass) | pronto |
| Render preview 540p / export 1080p, crop central | pronto |
| CLI `process` / `render` e testes | pronto |
| venv | **recriada em Python 3.14**: ctranslate2 4.8.2 e faster-whisper funcionam; 10 testes verdes |
| Integração Gemini (`app/pipeline/llm_gemini.py`, `clipstudio gemini-check`) | pronta; **falta testar com a chave real** |

## 1. Princípios

1. **Qualidade do corte em primeiro lugar.** Um corte bom começa com gancho, termina numa ideia concluída, não corta palavras e mantém o rosto no quadro. Toda a arquitetura existe para medir e melhorar isso.
2. **Custo baixo por padrão.**
   - Tudo que é pesado e determinístico roda localmente: decodificação, ASR, detecção de rosto e render.
   - O LLM recebe a transcrição em texto mais **amostras pequenas** de imagem e áudio, só dos trechos candidatos. Nunca recebe o vídeo inteiro.
   - O padrão é o **plano gratuito do Gemini**. Nenhuma chamada paga é feita sem sua autorização.
3. **IA plugável.** Cada etapa de IA é uma interface com pelo menos uma implementação local e uma por API, escolhida por configuração.
4. **Render determinístico.** `vídeo = f(fonte, EditSpec)`. Editar é mudar o `EditSpec` e re-renderizar uma nova versão; nada é "pintado" à mão.
5. **Etapas idempotentes e retomáveis.** Cada estágio grava seu artefato em disco. Se cair no meio, recomeça do último estágio concluído.
6. **Rodar no hardware atual.** São 2 núcleos, 3,6 GB de RAM e nenhuma GPU. Escalar depois troca componentes, sem reescrever o sistema.

## 2. Visão geral

```
┌──────────────────────┐   HTTPS/JSON    ┌────────────────────────┐
│ Android (Kotlin/     │ ──────────────> │ API (FastAPI)          │
│ Compose)             │ <────────────── │  auth por token        │
│ • upload retomável   │   progresso     │  projetos / cortes     │
│   (WorkManager)      │   (polling)     │  upload PUT+Range      │
│ • lista/preview      │                 │  download Range        │
│   (Media3)           │                 └──────────┬─────────────┘
│ • editor simples     │                            │ SQLite (WAL)
│ • MediaStore         │                 ┌──────────▼─────────────┐
└──────────────────────┘                 │ Worker (processo       │
                                         │ separado)              │
                                         │  fila em tabela,       │
                                         │  lease/heartbeat       │
                                         │  pipeline por estágio  │
                                         └──────────┬─────────────┘
                                                    │
        ┌──────────────────┬──────────────────┬─────┴─────────────┬──────────────┐
        ▼                  ▼                  ▼                   ▼              ▼
  ffmpeg/ffprobe     Transcriber        Curator             FaceTracker     Renderer
  (local)            local: faster-     API: Gemini Flash   local: OpenCV   ffmpeg +
                     whisper            (alt.: Claude,      YuNet (ONNX)    Pillow
                                        Ollama)
                                        fallback: heurística
                                                    │
                                         Armazenamento: data/projects/<id>/...
                                         (disco local → S3 na produção)
```

## 3. Tecnologias

| Camada | Escolha | Por quê | Alternativa futura |
|---|---|---|---|
| Linguagem do backend | **Python 3.14** (o do sistema; validado com ctranslate2 4.8.2) | ecossistema de mídia/IA | fixar a versão via `uv`, se algum wheel faltar |
| API | **FastAPI + Uvicorn** | async, OpenAPI gerada (o cliente Android consome o schema) | – |
| Persistência | **SQLite (WAL) + SQLAlchemy 2** | zero infraestrutura; basta para 1 máquina | Postgres |
| Fila | **tabela `jobs` no SQLite** com lease/heartbeat | sem Redis; robusta para 1 worker | Redis/RQ ou Celery |
| Arquivos | disco local `data/projects/<id>/` | simples | S3/MinIO + URLs assinadas |
| Mídia | **ffmpeg 7** (sem libass) + **Pillow** | já validado na Etapa 1 | libass, se disponível |
| ASR | **faster-whisper** `small` int8 na CPU | melhor qualidade/RAM localmente; timestamps por palavra | `medium`/`large-v3` em GPU; ASR por API |
| Curadoria | **Gemini Flash via `google-genai`** (modelo na decisão D1), com saída JSON validada por Pydantic, fallback automático para Flash-Lite em 429 e heurística local | multimodal (texto, imagem, áudio); plano gratuito; barato no plano pago | Claude (`LLM_PROVIDER=claude`), Ollama |
| Rosto/reframe | **OpenCV DNN + YuNet (ONNX, ~300 KB)** | leve, CPU, sem dependências pesadas | MediaPipe, InsightFace com GPU |
| Cenas | filtro `select='gt(scene,…)'` do ffmpeg | já disponível, sem dependência extra | PySceneDetect |
| Loudness | `ebur128` / `loudnorm` do ffmpeg | áudio uniforme entre cortes | – |
| Android | **Kotlin, Compose, Retrofit/OkHttp, WorkManager, Media3, Coil** | nativo; upload e download em segundo plano confiáveis | – |
| Testes | pytest (unitário + integração com vídeo sintético) e um **conjunto de avaliação** de qualidade | ver seção 6 | – |

## 4. Pipeline de processamento

Cada estágio lê os artefatos do anterior e grava os seus em `data/projects/<id>/`. O worker persiste `stage` e `progress`.

| # | Estágio | Entrada → saída | Detalhes |
|---|---|---|---|
| 1 | **INGEST** | upload → `source.<ext>` | Upload retomável próprio: `POST /uploads` → `PUT` com `Content-Range` em partes de 8–32 MiB → `308` + `Range`. Valida tamanho e duração máximos. |
| 2 | **PROBE / NORMALIZE** | `source` → `probe.json`, `audio.wav` (16 kHz mono), `proxy.mp4` (360p) | O proxy acelera a análise de rosto e cena. O export sempre usa a fonte original. |
| 3 | **TRANSCRIBE** | `audio.wav` → `transcript.json` / `.srt` / `.txt` | Palavras com tempos, VAD, idioma detectado ou forçado. Interface `Transcriber`. |
| 4 | **ANALYZE (sinais)** | áudio/proxy → `signals.json` | Fronteiras de frase e pausas, energia RMS por janela, velocidade de fala, cortes de cena, (opcional) presença de rosto por segundo. Tudo local e barato. |
| 5 | **CURATE (passo 1: texto e áudio)** | transcrição + sinais → `candidates.json` | **Uma chamada ao Gemini Flash com a transcrição inteira**, numerada por frase com `[mm:ss]` e marcas de pausa/energia. O LLM devolve JSON validado: frases de início e fim, título, gancho, motivo e scores (hook, coerência, valor, tendência). Pede 2 a 3 vezes mais candidatos do que o necessário. O prompt do usuário e as palavras-chave entram como critério. Uma hora de fala tem ~15–20 mil tokens, o que cabe folgado numa chamada. Acima de ~2–3 h, divide em blocos com sobreposição e faz uma passada final de ranqueamento. **Fallback:** heurística local, se não houver chave, se der erro ou em modo offline. |
| 5b | **CURATE (passo 2: verificação visual e de áudio)** | candidatos + proxy → `candidates.json` reordenado | Ver seção 4.1. Só para os ~12–15 melhores candidatos. |
| 6 | **REFINE** | candidatos → `clips.json` (`EditSpec` por corte) | Encaixa nas fronteiras de palavra/frase, com folga de 0,15–0,3 s. Remove silêncio inicial. Garante a duração. Dá preferência a terminar em pausa ou ponto final. Remove duplicatas por sobreposição. Ranqueia por `score = LLM overall` ajustado pelos sinais (penaliza baixa densidade de fala e longos trechos sem rosto). |
| 7 | **REFRAME** | proxy + janelas dos cortes → trajetória de crop no `EditSpec` | Roda **só nas janelas escolhidas**, não no vídeo inteiro. YuNet a 3–5 fps no proxy; o rosto é associado ao falante pelo tamanho e pela persistência. A trajetória é suavizada (EMA e zona morta, para a câmera não "tremer"). Em troca de cena, salta direto. Sem rosto, usa crop central (ou um layout com borda desfocada, decisão D4). O resultado é uma lista de keyframes `(t, cx, cy, escala)`, usada pelo ffmpeg com `crop` + `sendcmd`, ou cortando em segmentos com crop fixo e concatenando. |
| 8 | **CAPTIONS** | palavras do corte + `CaptionStyle` → PNGs + roteiro do concat | Páginas de 2–3 palavras com destaque na palavra ativa (pipeline atual). As edições de texto vêm de `word_overrides`. |
| 9 | **RENDER** | fonte + `EditSpec` → `clip_NN_v{versão}_{preview\|export}.mp4` + `.jpg` | Preview em 540×960 (`veryfast`, CRF 28), gerado para todos os cortes. Export em 1080×1920 (`medium`, CRF 20), **sob demanda**. H.264/AAC, `+faststart`, loudness normalizado em -14 LUFS. |
| 10 | **QA automático** | arquivos renderizados → `qa.json` | Duração confere; sem frames pretos (`blackdetect`); sem silêncio longo (`silencedetect`); legenda dentro da área segura; tamanho do arquivo razoável. Um corte que falha é marcado e não aparece como pronto. |
| 11 | **COMPLETE** | – | Notifica via status; o cliente baixa com Range. |

**Custo de reprocessar.** Editar um corte (trim, legenda, proporção) volta só aos estágios 7–9 daquele corte, com `Clip.version + 1`. Mudar as preferências de curadoria volta aos estágios 5–9, reaproveitando a transcrição e os sinais.

### 4.1 Análise multimodal dos melhores momentos

Mandar o vídeo inteiro ao Gemini custaria muitos tokens. Uma hora de vídeo fica na casa de centenas de milhares a ~1 milhão de tokens. Isso estoura a cota gratuita e envia conteúdo demais a terceiros. Por isso, a análise é feita **em funil**:

| Camada | Onde roda | O que extrai | Custo |
|---|---|---|---|
| **Transcrição** | local (faster-whisper) | palavras com tempos, frases, pausas | US$ 0 |
| **Sinais de áudio** | local (ffmpeg e numpy) | energia RMS por frase (percentil), velocidade de fala, pausa antes da frase, picos de volume (risadas, aplausos, ênfase) e loudness | US$ 0 |
| **Sinais visuais básicos** | local (ffmpeg e YuNet no proxy de 360p) | cortes de cena, presença e tamanho do rosto por segundo, quadros pretos ou estáticos | US$ 0 |
| **Passo 1: texto** | Gemini Flash | A transcrição numerada vem anotada com marcas compactas dos sinais acima, por exemplo `↑energia`, `pausa 1,2s`, `sem rosto`. O Gemini devolve de 12 a 15 candidatos com scores de gancho, coerência, valor e tendência. | ~15–25 mil tokens por hora de vídeo |
| **Passo 2: visual** | Gemini Flash | Para cada candidato, uma **folha de contato** gerada localmente: uma grade 3×3 de quadros com o tempo marcado, criada com o filtro `tile` do ffmpeg. Vai junto o trecho da transcrição. O modelo avalia o interesse visual (expressão, ação, demonstração, tela ou slide legível) e problemas (rosto fora, tela estática, quadro ruim). Tudo vai numa única requisição com todas as folhas. | ~5–20 mil tokens |
| **Passo 2b: áudio** (opcional, decisão D10) | Gemini Flash | Áudio comprimido de cada candidato, mono, com baixa taxa de bits. Avalia o tom, as risadas, a música e a qualidade da fala. | ~32 tokens/s, ~20 mil tokens para 15 candidatos de 45 s |
| **Score final** | local | `score = 0,6·LLM_texto + 0,25·LLM_visual + 0,15·sinais_locais`, recalibrado com o conjunto de avaliação (seção 6) | – |

Ao todo são **de 2 a 3 requisições por vídeo**, mais ~40–60 mil tokens por hora de vídeo. Os pesos e o número de candidatos são parâmetros ajustados pelo `clipstudio eval`. Se o passo 2 falhar (cota, rede), o pipeline segue só com o passo 1, sem bloquear.

### Interfaces de IA (provider-agnostic)

```python
class Transcriber(Protocol):
    def transcribe(self, wav: Path, language: str | None) -> Transcript: ...

class Curator(Protocol):
    def curate(self, transcript: Transcript, signals: Signals, prefs: CurationPrefs) -> list[ClipCandidate]: ...

class FaceTracker(Protocol):
    def track(self, video: Path, start: float, end: float) -> list[FaceBox]: ...
```

Implementações escolhidas por `.env`: `TRANSCRIBER=local|api`, `LLM_PROVIDER=gemini|claude|none`, `FACE_TRACKER=yunet|none`. Já implementado: `LLM_PROVIDER`, `GEMINI_MODEL` e `GEMINI_FALLBACK_MODEL`. O `Curator` com LLM sempre recebe o mesmo prompt e o mesmo schema Pydantic. Trocar de provedor é trocar só o cliente HTTP.

## 5. Modelo de dados e API

**Tabelas.**

| Tabela | Colunas |
|---|---|
| `projects` | id, título, status, stage, progress, erro, prefs (JSON), criado/atualizado |
| `uploads` | id, project_id, tamanho, recebidos, sha256, estado |
| `jobs` | id, project_id, tipo (`process`/`render`), clip_id?, estado, tentativas, `lease_until`, payload |
| `clips` | id, project_id, rank, score, scores (JSON), título, gancho, `edit_spec` (JSON), versão, `preview_path`, `export_path`, `qa` (JSON) |

**Endpoints.** Todos com `Authorization: Bearer <token>`.

| Método | Caminho | Uso |
|---|---|---|
| POST | `/projects` | cria o projeto com prefs; devolve `upload_id` |
| PUT | `/uploads/{id}` | partes com `Content-Range`; `308` + `Range` |
| PUT | `/uploads/{id}` (corpo vazio, `bytes */N`) | consulta o offset para retomar |
| POST | `/projects/{id}/start` | enfileira o processamento |
| GET | `/projects`, `/projects/{id}` | lista e status (stage, progress, erro) |
| DELETE | `/projects/{id}` | apaga o projeto e os arquivos |
| GET | `/projects/{id}/clips` | cortes ranqueados |
| PATCH | `/clips/{id}` | edita o `EditSpec` (incrementa a versão) |
| POST | `/clips/{id}/render?quality=export` | enfileira o export |
| GET | `/clips/{id}/file?kind=preview\|export&v=N` | download com `Range`/`206` |
| GET | `/clips/{id}/thumb`, `/projects/{id}/transcript.srt` | miniatura e transcrição |

## 6. Como garantir a qualidade dos cortes (mensurável)

Sem medição, "qualidade" vira opinião. Proposta:

1. **Conjunto de avaliação.** De 8 a 12 vídeos reais seus (podcast, aula, entrevista, vlog), em pt-BR, de 10 a 60 min. Para cada um, você marca de 3 a 6 trechos "eu postaria isto", só início e fim aproximados.
2. **Métricas automáticas** (comando `clipstudio eval`):

   | Métrica | O que mede |
   |---|---|
   | *Recall@K* | dos trechos marcados, quantos aparecem nos K primeiros cortes (sobreposição ≥50%) |
   | Fronteiras | % de cortes que começam e terminam fora de uma palavra; % que terminam em pausa ou pontuação |
   | Reframe | % de frames com o rosto dentro do crop, e jitter (deslocamento médio do crop por segundo) |
   | QA técnico | taxa de falhas da seção 4, estágio 10 |
   | Custo e tempo | tokens e US$ por hora de vídeo; tempo de parede por estágio |

3. **Avaliação humana rápida.** Nota de 1 a 5 em cada corte dos 5 primeiros, com o mesmo formulário a cada versão do prompt ou modelo.
4. **Regra.** Mudanças de prompt, modelo ou heurística só entram se não piorarem o Recall@5 nem a nota média no conjunto.

## 7. Custos estimados

**LLM (Gemini).** Considera ~40–60 mil tokens de entrada e ~5–10 mil de saída (com raciocínio) por hora de vídeo, já com os passos 1 e 2. Preços oficiais do plano pago até 2026-12-31, por milhão de tokens. Consultados em ai.google.dev/gemini-api/docs/pricing em 2026-10-12:

| Modelo | Plano gratuito | Pago (entrada / saída) | ≈ custo pago por hora de vídeo |
|---|---|---|---|
| Gemini 3.8 Flash | sim | $0,75 / $3,75 | ~US$ 0,07–0,08 |
| Gemini 3.5 Flash-Lite | sim | $0,30 / $2,50 | ~US$ 0,03–0,04 |
| Gemini 2.5 Flash-Lite | sim | $0,10 / $0,40 | < US$ 0,01 |

- **No plano gratuito, o custo é US$ 0**, limitado pelas cotas do *projeto*: requisições por minuto, por dia e tokens por minuto. Os números dependem do projeto e aparecem em aistudio.google.com/rate-limit. A API não informa isso.
- **Atenção à privacidade:** no plano gratuito, o Google declara que o conteúdo enviado pode ser usado para melhorar os produtos dele. No pago, não.
- Os preços do Flash sobem a partir de 2027-01-01. Rever se ativar o plano pago.
- O custo real é registrado a cada chamada via `usage_metadata`.

**Infraestrutura no MVP.** US$ 0: roda neste PC.

**Tempo na CPU atual (i3, 2C/4T).**

- O ASR `small` int8 deve levar aproximadamente o tempo real do vídeo ou mais.
- O reframe só nas janelas escolhidas deve levar segundos ou minutos.
- O preview de cada corte deve levar de 30 s a 2 min.

Tudo isso **precisa ser medido** no passo M0. Se ficar lento demais, as opções são ASR `base`, ASR por API, ou uma máquina melhor ou VPS para o worker.

## 8. Etapas até o MVP

**MVP =** pelo celular Android, enviar um vídeo de até 60 min, acompanhar o progresso, ver de 5 a 8 cortes 9:16 com legenda animada e rosto enquadrado, ajustar início/fim e o texto da legenda, exportar em 1080p e salvar na galeria.

| Passo | Entrega | Critério de aceite |
|---|---|---|
| **M0. Ambiente e IA** | ✅ venv em Python 3.14. ✅ Tempos medidos (vídeo de 96 s, i3): transcrição 141 s, análise 24 s, previews 35 s, export ~17 s por corte. ✅ Gemini no plano gratuito (2026-10-13): a chave acessa 45 modelos, incluindo 3.8 Flash e 3.5 Flash-Lite; ping OK; curadoria real em 31 s. Retentativa em 5xx e fallback de modelo; a IA é completada pela curadoria local quando entrega poucos cortes | ✅ |
| **M1. Conjunto de avaliação** | ✅ `clipstudio eval` (Recall@3/5, Precision@3, cortes no meio de palavra, término em pontuação, modos de enquadramento, jitter, tempos). ⏳ Faltam os vídeos reais marcados por você; hoje só há o conjunto sintético `app/data_test/eval_sintetico.json` | relatório de baseline com vídeos reais |
| **M2. Qualidade do motor** | ✅ ANALYZE (cortes de cena por histograma e luminância, rostos a 2,5 fps), curadoria v2 (gancho, pausas, conectivos, energia, rosto, ritmo de cenas), refine sem cortes repetidos, QA automático, loudness a -14 LUFS | 0% de cortes no meio de palavra e 100% terminando em pontuação no conjunto sintético; Recall precisa de vídeos reais |
| **M3. Reframe por rosto** | ✅ YuNet, câmera virtual (zona morta, velocidade máxima, salto na troca de cena), trajetória no `EditSpec`, modos FIT e CENTER como alternativa | validado com rostos reais pequenos e com mascote animado; ⏳ falta medir em vídeo de pessoa falando para a câmera |
| **M4. API + worker** | ✅ FastAPI, SQLite WAL, fila com lease/heartbeat, upload retomável (308 + Range), download com Range, token fixo, edição versionada, cliente de referência `scripts/api_smoke.py` | ✅ teste automatizado e teste real com uvicorn e worker em processos separados |
| **M5. Android: envio e progresso** | ✅ `app/android` (Kotlin, Compose, AGP 9.3.2): configurações (URL e token, testar conexão), seletor de vídeo do sistema, `UploadWorker` (WorkManager em primeiro plano, partes de 8 MiB, retomada por 308 + Range, espera exponencial) e lista e detalhe com progresso, fila e erros | ✅ APK compila; testes JVM do upload (queda de conexão, 503, erro definitivo) e teste contra o backend real (101 s); ⏳ teste em aparelho físico (o emulador não roda neste PC) |
| **M6. Android: cortes e exportação** | ✅ lista de cortes com miniatura, nota e QA; player Media3 autenticado; `ExportWorker` (render 1080p, download com Range retomável, MediaStore `Movies/ClipStudio`, compartilhar e abrir) | ✅ telas verificadas com Robolectric e capturas de tela; ⏳ salvar na galeria de um aparelho real |
| **M7. Editor mínimo** | ✅ trim (RangeSlider), enquadramento, legendas (ligar/desligar, CAIXA ALTA, cor de destaque, altura) e correção de palavras (`word_overrides`); salvar gera nova versão e prévia no servidor sem reprocessar | ✅ teste de UI confirma o PATCH enviado; backend testado com edição, nova versão e prévia |
| **= MVP** | – | **teste de ponta a ponta com 3 vídeos reais** |

M1–M3 (qualidade) vêm antes de M4 (API) de propósito. São o diferencial do produto e podem ser validados pela CLI, sem depender do app.

**Fora do MVP (Etapa de produção):**

- contas e login;
- Postgres, S3, Docker e Redis;
- cotas e cobrança;
- importação por URL;
- B-roll, emojis e tradução;
- múltiplos falantes com troca de layout;
- web app.

## 9. Riscos

| Risco | Mitigação |
|---|---|
| CPU e RAM fracas tornam o processamento lento ou causam OOM | Proxy de 360p; ASR `small`/`base`; um job por vez; reframe só nas janelas; opção de ASR por API ou worker em VPS |
| LLM escolhe trechos ruins ou alucina índices | Schema validado; índices conferidos com a transcrição; refine determinístico; eval antes de cada mudança; fallback heurístico |
| Reframe "tremido" ou no rosto errado | Suavização + zona morta; associação por persistência; QA de jitter; o usuário pode fixar o crop central no editor |
| ffmpeg sem libass/drawtext | Pipeline Pillow já validado; independe do build |
| Direitos autorais (vídeos de terceiros) | MVP só com upload de arquivos próprios (decisão D3) |
| Vazamento da chave da API | A chave vem só de `GEMINI_API_KEY` (ambiente ou `.env`, fora do git), é tratada como `SecretStr` e nunca aparece em log; o app Android nunca vê a chave |
| Cota gratuita do Gemini esgotada (429) | Fallback automático para o modelo Flash-Lite; depois, curadoria heurística local; passo 2 opcional |
| Uso dos dados enviados no plano gratuito | Enviar só transcrição e amostras dos candidatos; decisão D11 |
| Modelos Gemini mudam ou são descontinuados | Nome do modelo no `.env`; `clipstudio gemini-check` lista o que a chave acessa |

## 10. Decisões que precisam da sua aprovação

Já aprovadas e mantidas: cliente **Android nativo** e execução **local** (SQLite, disco, sem Docker). **Alterada pela revisão 2:** a curadoria passa a usar o **Gemini** com fallback heurístico; o Claude fica opcional.

| # | Decisão | Opções | Recomendação |
|---|---|---|---|
| **D1** | Modelo Gemini da curadoria | (a) **Gemini 3.8 Flash** como principal, com fallback para 3.5 Flash-Lite; (b) só Flash-Lite, que costuma ter mais cota gratuita e qualidade menor | **(a)**, se `gemini-check` confirmar que a sua chave acessa ambos. O conjunto de avaliação do M1 mede se o Flash-Lite basta |
| **D2** | Transcrição | (a) **local faster-whisper `small`**: US$ 0, mais lento; (b) `medium` local: melhor, mas arrisca a RAM; (c) ASR por API: rápido, custo por minuto | **(a)**, com a interface pronta para (c) se o tempo medido no M0 for inaceitável |
| **D3** | Origem dos vídeos no MVP | (a) **só upload de arquivo**; (b) também importar por URL (YouTube etc., via yt-dlp) | **(a)**. Importar por URL tem implicações de termos de uso e direitos; fica para depois, se você quiser |
| **D4** | Reframe por rosto no MVP | (a) **sim, YuNet local** (antecipa a antiga Etapa 6); (b) não, só crop central | **(a)**: é o que mais pesa na percepção de qualidade em 9:16. Sem rosto: crop central (ou fundo desfocado, se preferir) |
| **D5** | Onde o backend roda para testar com o celular | (a) **neste PC, na rede local (Wi-Fi)**; (b) VPS barata; (c) túnel (ex.: Tailscale) | **(a)** no MVP; (c) se precisar testar fora de casa |
| **D6** | Autenticação no MVP | (a) **token fixo** no `.env`, digitado no app; (b) contas com login | **(a)**: uso pessoal; contas ficam para produção |
| **D7** | Ordem das etapas | (a) **qualidade primeiro** (M1–M3 pela CLI, depois API e Android); (b) app primeiro (M4–M6, depois qualidade) | **(a)** |
| **D8** | Conjunto de avaliação | Você fornece de 8 a 12 vídeos próprios e marca os melhores trechos (≈1–2 h do seu tempo) | Necessário para "priorizar qualidade" ser verificável. Posso começar com 3 vídeos |
| **D9** | Idioma e estilo padrão | pt-BR; legendas em CAIXA ALTA, 2–3 palavras, destaque amarelo | Manter, editável por preset |
| **D10** | Passo 2b (enviar o áudio dos candidatos ao Gemini) | (a) desligado no MVP; (b) ligado | **(a)** no início; ligar se o eval mostrar ganho |
| **D11** | Plano gratuito do Gemini e uso dos dados | (a) **gratuito**, aceitando que o Google pode usar o conteúdo enviado; (b) pago, com dados não usados e custo de ~US$ 0,03–0,08 por hora de vídeo | **(a)** para os testes com vídeos seus; reavaliar antes de processar vídeos de terceiros |

Com as decisões aprovadas (ou ajustadas), o próximo passo é concluir o **M0**: rodar `clipstudio gemini-check` com a sua chave, fazer o ping no plano gratuito e medir os tempos em um vídeo real seu. Depois vêm M1 a M3.
