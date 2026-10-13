# ClipStudio: app Android (M5–M7)

App nativo em Kotlin e Jetpack Compose. Ele conversa com o backend (`../backend`) pela API HTTP. O app não processa vídeo: envia o vídeo, acompanha o progresso, mostra e edita os cortes e salva na galeria.

| Tela | O que faz |
|---|---|
| Configurações | Endereço do servidor (IP do PC na rede local) e o `API_TOKEN`; tem botão "Testar conexão" |
| Projetos | Lista com status. "Novo vídeo" abre o seletor de fotos do sistema, sem permissão de armazenamento, e depois a tela de preferências (duração, quantidade, idioma, enquadramento) |
| Projeto | Progresso do envio e do processamento (estágio, fila, %); depois, a lista de cortes com miniatura, nota e QA |
| Corte | Player (Media3) da prévia; export 1080p; salvar em `Filmes/ClipStudio`; compartilhar |
| Editor | Trim (RangeSlider), enquadramento (auto, rosto, quadro inteiro, centro), legendas (ligar/desligar, CAIXA ALTA, cor da palavra ativa, altura na tela) e correção de palavras. Ao salvar, gera nova versão e prévia |

Comportamento em segundo plano:
- **Upload** (`UploadWorker`, WorkManager em primeiro plano, tipo `dataSync`):
  - envia em partes de 8 MiB com `Content-Range`;
  - retoma do offset informado pelo servidor (308 + `Range`);
  - repete com espera exponencial;
  - sobrevive a troca de rede e ao app fechado;
  - ao terminar, inicia o processamento.
- **Export** (`ExportWorker`):
  - pede o render 1080x1920 e espera terminar;
  - baixa com `Range`, retomando do `.partial`;
  - grava no MediaStore com `IS_PENDING`.

## Build
Requer JDK 17 e o Android SDK (platform 37 e build-tools 36).
```bash
./gradlew assembleDebug            # app/build/outputs/apk/debug/app-debug.apk
./gradlew testDebugUnitTest        # contrato da API, upload retomável (MockWebServer) e telas (Robolectric)
                                   # capturas das telas em app/build/screenshots/
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Teste opcional contra o backend real (servidor e worker no ar):
```bash
CLIPSTUDIO_URL=http://127.0.0.1:8000 CLIPSTUDIO_TOKEN=seu-token CLIPSTUDIO_VIDEO=/caminho/video.mp4 \
  ./gradlew testDebugUnitTest --tests '*LiveServerTest*'
```

## Usando com o backend
1. No PC, rode `uvicorn app.api.main:app --host 0.0.0.0 --port 8000` com `API_TOKEN` definido, e o worker em outro terminal.
2. No app, informe `http://<IP-do-PC>:8000` e o token. No emulador, o PC é `http://10.0.2.2:8000`.
3. O MVP usa HTTP na rede local (`network_security_config` permite cleartext). Para produção, usar HTTPS e remover isso.

## Limitações conhecidas
- Ainda não foi testado num aparelho físico. O emulador (API 37) exige 4 GB de RAM, e no PC de desenvolvimento (3,6 GB) ele travou o sistema. As telas foram verificadas com Robolectric, e o protocolo com o backend real.
- Miniaturas e prévias exigem o servidor acessível na rede; não há cache offline.
