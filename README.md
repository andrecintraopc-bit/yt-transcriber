# yt-transcriber

Transcreve vídeos do YouTube e gera Markdown com resumo, timestamps e notas de estudo.

## Como funciona

1. Tenta baixar legendas do YouTube (rápido, sem Whisper)
2. Se não tem legenda → baixa áudio e transcreve com `faster-whisper` (local, gratuito)
3. Gera resumo via `claude -p --model haiku` (requer Claude Code CLI)
4. Salva `.md` em `~/yt-transcricoes/`

## Instalação

```bash
pip install faster-whisper yt-dlp
```

## Uso

```bash
# URL única
python yt_transcribe.py "https://youtube.com/watch?v=..."

# Com timestamps e notas de estudo
python yt_transcribe.py "https://youtube.com/watch?v=..." --timestamps --study

# Batch (arquivo .txt com 1 URL por linha)
python yt_transcribe.py --file urls.txt

# Alta qualidade (modelo medium, mais lento)
python yt_transcribe.py "..." --quality high

# Vídeo em inglês
python yt_transcribe.py "..." --lang en

# Trechos de alta densidade para reel (30-60s)
python yt_transcribe.py "..." --reels
```

## Opções

| Flag | Descrição |
|------|-----------|
| `--quality low\|high` | `low` = modelo small (rápido), `high` = medium (mais preciso) |
| `--lang pt\|en` | Idioma do resumo gerado |
| `--timestamps` / `-t` | Adiciona `[HH:MM:SS]` em cada linha |
| `--study` / `-s` | Gera notas de estudo (bullets, glossário, perguntas-chave) |
| `--reels` / `-r` | Lista trechos de alta densidade (30-60s) |
| `--speakers` | Identifica quem fala (requer `pip install pyannote.audio torch`) |
| `--output` / `-o` | Nome do arquivo de saída (sem `.md`) |
| `--file` / `-f` | Arquivo `.txt` com URLs |

## Configuração via env

```bash
export YT_OUTPUT_DIR=~/Documentos/transcricoes   # padrão: ~/yt-transcricoes
export YT_TEMP_DIR=/tmp/yt_transcriber            # padrão: /tmp/yt_transcriber
```

## Saída

```
~/yt-transcricoes/
├── transcricoes_2026-05-04_143022.md
├── transcricoes_2026-05-04_143022.json
└── history.db                          # SQLite (índice de transcrições)
```

O Markdown contém: título, canal, duração, link, resumo, e transcrição completa.
Com `--study`: bullets de insights, glossário técnico, perguntas-chave.
Com `--timestamps`: cada linha com `[HH:MM:SS]`.

## Histórico SQLite (`history.db`)

CLI e API web populam a mesma tabela `history` em `~/yt-transcricoes/history.db`.
Schema:

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `job_id` | TEXT | uuid4 (CLI) ou job FastAPI (API) |
| `created_at` | TEXT | ISO timestamp |
| `title` | TEXT | título do vídeo |
| `channel` | TEXT | canal YouTube |
| `url` | TEXT | URL original |
| `summary` | TEXT | resumo Haiku (truncado a 300 chars) |
| `md_path` | TEXT | path absoluto do `.md` gerado |

Consulta direta:
```bash
sqlite3 ~/yt-transcricoes/history.db \
  "SELECT id, substr(title,1,40), substr(channel,1,20) FROM history ORDER BY id DESC LIMIT 10;"
```

Backfill de JSONs antigos (idempotente):
```bash
./.venv/bin/python backfill_history.py
```

API web (`app.py`): `GET /history?q=<termo>` retorna últimas 50 linhas
filtradas por `title` ou `summary`.

## Gotcha — Paralelismo

`yt_transcribe.py` usa `glob("*.m4a")` no `YT_TEMP_DIR` para localizar o áudio
baixado. Rodar 2 instâncias em paralelo na mesma máquina **com o mesmo TEMP_DIR**
causa race condition (uma instância pode pegar o m4a da outra). Para paralelismo,
isolar via env var:

```bash
YT_TEMP_DIR=/tmp/yt_a python yt_transcribe.py "URL_A" &
YT_TEMP_DIR=/tmp/yt_b python yt_transcribe.py "URL_B" &
```
