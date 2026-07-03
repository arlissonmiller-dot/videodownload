# Project Memory

## Resumo
- App local para baixar videos usando `yt-dlp`, com frontend React/Vite e backend FastAPI.
- O projeto começou focado em YouTube e foi adaptado para suportar Instagram tambem.
- O objetivo principal passou a ser: estabilidade, menor uso de memoria no navegador, mensagens melhores de erro e mais compatibilidade com os arquivos baixados.

## Arquitetura Atual
- `frontend/`: React + Vite.
- `backend/`: FastAPI + `yt-dlp` + `ffmpeg`.
- `start.sh`: sobe backend e frontend localmente em `127.0.0.1`.
- `setup.sh`: prepara backend/frontend e instala dependencias do projeto.

## Mudancas Importantes Ja Feitas

### Backend
- Removido o uso problemático de `youtube:player_client=android,web`.
- Adicionado fallback automatico sem cookies quando o `yt-dlp` falha com erros tipicos de cookies/client incompatível.
- Criada rota `/config` para o frontend descobrir:
  - modo de cookies
  - navegadores disponiveis
  - TTL de cache
  - limite de downloads simultaneos
  - runtime JS e challenge solver
- Tornado CORS configuravel via `CORS_ORIGINS`.
- Melhorado `Content-Disposition` para evitar erro de encoding `latin-1`.
- Adicionado cache TTL para `/info`.
- Adicionado sistema de jobs de download:
  - `POST /download-jobs`
  - `GET /download-jobs?limit=N`
  - `GET /download-jobs/{job_id}`
  - `POST /download-jobs/{job_id}/retry`
  - `GET /download-jobs/{job_id}/events`
  - `GET /download-jobs/{job_id}/file`
- Adicionado limite de concorrencia com `BoundedSemaphore`.
- Adicionado progresso via parsing do stdout do `yt-dlp`.
- Adicionada persistencia de jobs em SQLite (`backend/jobs.db`) para o historico sobreviver a restart do backend.
- Mantido endpoint antigo `/download` para compatibilidade.
- Generalizada a deteccao de plataforma:
  - YouTube
  - Instagram
  - Facebook
  - fallback generico
- Generalizada a montagem de formatos para nao depender só de `height`.
- Mensagens de erro ficaram mais amigaveis para bloqueios anti-bot, login necessario e validacao extra.

### yt-dlp / runtime
- `yt-dlp` local foi atualizado para versao nova (`2026.03.17` durante a sessao).
- `backend/requirements.txt` foi alterado para `yt-dlp[default]>=2025.10.14`.
- Instalado `yt-dlp-ejs` localmente na venv para challenge solver.
- Backend agora usa `node` como JS runtime quando disponivel.
- Quando nao existe solver local, o backend pode usar `--remote-components ejs:github`.

### Frontend
- O app deixou de baixar via `fetch(...).blob()` como fluxo principal.
- Agora o frontend:
  - cria um job de download
  - acompanha progresso via SSE
  - dispara o download final pelo navegador
- Adicionado historico local de URLs recentes com `localStorage`.
- Adicionada secao de downloads recentes persistidos pelo backend.
- Adicionada acao de repetir download a partir do historico.
- Adicionados atalhos rapidos de formato:
  - Melhor
  - 1080p
  - 720p
  - MP3
- Adicionado card de progresso do download.
- Adicionados indicadores de:
  - cache
  - downloads simultaneos
  - runtime JS
  - challenge solver
- A interface foi renomeada de `YouTube Downloader` para `Video Downloader`.
- UI ajustada para aceitar links de YouTube ou Instagram.
- Exibicao de `platform_label` no card da midia.

### Infra / execucao local
- `start.sh` foi ajustado para:
  - usar `127.0.0.1`
  - evitar `--reload` por padrao
  - aceitar variaveis `BACKEND_HOST`, `BACKEND_PORT`, `FRONTEND_HOST`, `FRONTEND_PORT`, `ENABLE_RELOAD`
- `frontend/vite.config.js` foi ajustado para:
  - `host: 127.0.0.1`
  - `strictPort: true`
  - proxy de `/config`, `/info`, `/download`, `/download-jobs`
- `frontend/nginx.conf` tambem foi ajustado para proxiar `/download-jobs`.
- `frontend/Dockerfile` foi ajustado para usar `npm ci` e aceitar `VITE_API_URL`.
- `backend/Dockerfile` foi ajustado para instalar `ffmpeg`, `nodejs` e `npm`.

## Problemas Ja Encontrados e Como Foram Tratados

### 1. Cookies + YouTube
- Erro: `Skipping client "android" since it does not support cookies`.
- Solucao: parar de forcar esse `player_client` e deixar o `yt-dlp` escolher clientes melhores.

### 2. Erro de encoding no download
- Erro: `latin-1 codec can't encode character`.
- Solucao: gerar `filename` ASCII seguro + `filename*` UTF-8.

### 3. Frontend/dev server falhando ao subir
- Problema com bind e `--reload`.
- Solucao: padronizar em `127.0.0.1` e desabilitar `reload` por padrao.

### 4. YouTube bloqueando com 429 / anti-bot
- Solucao:
  - mensagens melhores
  - suporte a cookies
  - runtime JS com node
  - `yt-dlp-ejs`

### 5. Instagram baixando so audio
- Solucao:
  - seletor de video mais explicito
  - escolha melhor do arquivo final

### 6. Instagram gerando MP4 sem imagem / nao reproduzindo
- Solucao:
  - conversao para MP4 mais compativel
  - remux rapido primeiro
  - fallback para `h264_videotoolbox`
  - ultimo fallback com `libx264`

### 7. Instagram travando em 98%
- Causa mais provavel: etapa final de conversao.
- Solucao: pipeline de conversao em 3 tentativas, com timeout por tentativa.

## Estado Atual Conhecido
- Backend compila com `python -m py_compile`.
- Frontend gera build com `npm run build`.
- O ambiente deste chat nao consegue validar downloads reais com internet aberta, entao a validacao aqui foi estrutural/build + ajustes baseados em erros reais do usuario.

## Arquivos Mais Importantes
- `backend/main.py`
- `backend/requirements.txt`
- `backend/Dockerfile`
- `frontend/src/App.jsx`
- `frontend/src/App.css`
- `frontend/vite.config.js`
- `frontend/nginx.conf`
- `frontend/index.html`
- `start.sh`
- `setup.sh`

## Pontos de Atencao Futuros
- Instagram pode variar bastante por tipo de post/reel/carrossel.
- Alguns downloads podem continuar exigindo cookies validos.
- Se aparecer novo caso de travamento no Instagram, inspecionar:
  - formato real retornado pelo `yt-dlp`
  - codec do arquivo baixado
  - qual tentativa do `ffmpeg` foi usada
- Se quiser persistencia melhor dos jobs, o proximo passo natural seria salvar estado em SQLite.

## Como Usar Esta Memoria
- Ao retomar o projeto, leia este arquivo primeiro.
- Considere `backend/main.py` como centro da logica de download.
- Considere `frontend/src/App.jsx` como centro do fluxo de UX.
