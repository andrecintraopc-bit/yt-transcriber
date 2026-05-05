# ADR 0001 — SQLite local + módulo `db.py` compartilhado para histórico de transcrições

- **Status**: Aceito
- **Data**: 2026-05-05
- **Decisor**: André Cintra (operador) + agente desta sessão

## Contexto

`yt-transcriber` gera dois artefatos por execução em `~/yt-transcricoes/`:
arquivo `.md` (Markdown legível) e arquivo `.json` (estrutura completa com
segmentos + metadados). Antes desta decisão:

- `app.py` (FastAPI, commit `fe03804`) já tinha schema SQLite `history.db`
  embutido — populado **somente** quando transcrição era disparada via UI web.
- `yt_transcribe.py` (CLI) salvava MD+JSON mas **não indexava** em banco.
- Operador relatou que o estado atual parecia "memória do app" (interpretação
  inicial errada — arquivos estavam no FS, persistentes; mas sem índice
  consultável o sentimento era de não-registrado).

`history.db` ainda **não existia em disco** — `app.py` nunca foi colocado em
produção (`yt-transcriber.service` no repo, mas não em `~/Library/LaunchAgents/`).

## Opções consideradas

| Opção | Descrição | Custo | Ganho |
|---|---|---|---|
| **A — Wire SQLite no CLI + extrair `db.py`** | Schema existente migra para módulo compartilhado; CLI e API usam o mesmo backend | ~5 linhas no CLI + extração + remoção de duplicação | Indexação local imediata, DRY, sem nova infra |
| B — Promover `app.py` a serviço | Daemon FastAPI permanente como único caminho válido | Manter daemon, expor porta, monitorar | Cobre só uso UI; CLI continuaria sem registry |
| C — Tabela Supabase remota | Tabela `yt_transcricoes` em `cintra-space` | Migration + env + auth | Sync entre Mac e VPS, queryável de qualquer agente |
| D — Merge no `repertoire_references.json` (music-study-pack) | Adicionar transcrições como anexos das músicas do setlist | Adapter custom + esquema acoplado | Faria sentido se transcrições fossem sempre sobre músicas do setlist — não é o caso (KB cajón é mais ampla) |

## Decisão

**Opção A.**

Implementação:
- Novo módulo `db.py` (init_db / save_history / get_history / DB_PATH)
- `yt_transcribe.py` chama `init_db()` + `save_history()` no fim de
  `process_urls()`. Aceita `job_id` opcional (uuid4 auto se ausente).
- `app.py` importa de `db.py` (73 linhas duplicadas removidas) e passa
  `job_id` real ao `process_urls()` para evitar double-insert.
- `backfill_history.py` 1-shot idempotente para popular DB com JSONs antigos.

## Razões

1. **Schema já existia** em `app.py` — zero design novo, só extração.
2. **Escopo é uso pessoal local** — não há requisito de sync multi-máquina
   nem de acesso por outros agentes da arquitetura Cintra.
3. **R$0** — nem MAX CLI (não chama IA) nem Supabase (não sobe pra rede).
4. **DRY**: extrair para `db.py` elimina risco futuro de drift entre cópia
   da CLI e cópia da API.
5. **Aderente a `~/.claude/rules/anti-overengineering.md`**: opção A tem
   menor superfície e maior reutilização do que existia. Opção C (Supabase)
   seria over-engineering para listar < 100 arquivos locais.

## Consequências

### Positivas
- CLI passa a indexar transcrições automaticamente.
- `app.py` mais enxuto (73 linhas a menos).
- `backfill_history.py` permite reconstrução incremental do índice.
- API web (se vier a rodar) e CLI escrevem na mesma tabela — consulta única.
- `get_history(q)` queryável diretamente via `sqlite3` CLI sem subir o app.

### Negativas / Limitações aceitas
- Não sincroniza entre Mac e VPS (decisão consciente — uso é Mac-local).
- `history.db` precisa backup manual (junto com `~/yt-transcricoes/`).
- Schema sem `UNIQUE(url, md_path)` — depende da lógica de
  `backfill_history.already_inserted()` para deduplicar. Se múltiplos
  processos escreverem em paralelo, pode haver duplicatas (aceitável: SQLite
  serializa writes; deduplicação via `already_inserted()` resolve em backfill).

### TODOs ortogonais (não fazem parte deste ADR)
- Race condition em `/tmp/yt_transcriber/` (`glob("*.m4a")`) é problema
  pré-existente do CLI ao rodar paralelo — documentado em
  `~/.claude/rules/gotchas-tecnicos.md`. Fix estrutural envolve passar
  `output_template` por video_id ao yt-dlp em `download_audio()`.
- Decisão sobre subir `app.py` como serviço fica em aberto (independente
  deste ADR — `db.py` funciona com ou sem o serviço).

## Cross-refs

- Commit `46b60da` — extração de `db.py` + wire-in no CLI
- Commit `aeba449` — documentação do schema no `README.md` + workaround paralelismo
- Schema da tabela `history`: `db.py` linhas 18–32
- `~/.claude/rules/anti-overengineering.md` — checklist aplicado para descartar opção C
- `~/.claude/rules/gotchas-tecnicos.md` (entry yt-transcriber 2026-05-05) — race condition
