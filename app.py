#!/usr/bin/env python3
"""
yt-transcriber — Web app (FastAPI + UI mobile-friendly)
Porta padrão: 8855
Features: histórico SQLite, chat sobre conteúdo, clipping, export SRT/TXT/JSON, playlist
"""

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from yt_transcribe import (
    process_urls, validate_url, expand_playlist, is_playlist_url,
    format_srt, OUTPUT_DIR, _haiku, YT_DLP,
)

COOKIES_PATH = OUTPUT_DIR / "cookies.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yt_transcriber.app")

app = FastAPI(title="yt-transcriber", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict = {}

# ---------------------------------------------------------------------------
# SQLite — Histórico
# ---------------------------------------------------------------------------

DB_PATH = OUTPUT_DIR / "history.db"


def _init_db():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            created_at TEXT,
            title TEXT,
            channel TEXT,
            url TEXT,
            summary TEXT,
            md_path TEXT
        )
    """)
    con.commit()
    con.close()


def _save_history(job_id: str, results_json: dict, md_path: str):
    try:
        con = sqlite3.connect(str(DB_PATH))
        for r in results_json.get("results", []):
            info = r.get("info", {})
            con.execute(
                "INSERT INTO history (job_id, created_at, title, channel, url, summary, md_path) VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    datetime.now().isoformat(),
                    info.get("title", ""),
                    info.get("channel", ""),
                    info.get("url", ""),
                    r.get("summary", "")[:300],
                    md_path,
                ),
            )
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"Falha ao salvar histórico: {e}")


def _get_history(q: str = "") -> list:
    try:
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        if q:
            rows = con.execute(
                "SELECT * FROM history WHERE title LIKE ? OR summary LIKE ? ORDER BY created_at DESC LIMIT 50",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM history ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


_init_db()

# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

UI_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>yt-transcriber</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #e8e8e8; padding: 16px; max-width: 700px; margin: 0 auto; }
  h1 { font-size: 1.2rem; font-weight: 600; margin-bottom: 4px; color: #fff; }
  .sub { font-size: 0.78rem; color: #888; margin-bottom: 20px; }
  .card { background: #1a1a1a; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  label { font-size: 0.8rem; color: #aaa; display: block; margin-bottom: 6px; }
  input[type=text], input[type=number], textarea {
    width: 100%; background: #111; border: 1px solid #333; border-radius: 8px;
    color: #fff; padding: 10px 12px; font-size: 0.95rem; outline: none;
  }
  input[type=text]:focus, input[type=number]:focus, textarea:focus { border-color: #555; }
  .row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; align-items: flex-start; }
  .toggle { display: flex; align-items: center; gap: 6px; font-size: 0.82rem; color: #ccc; }
  .toggle input { width: 16px; height: 16px; cursor: pointer; accent-color: #e63946; }
  select {
    background: #111; border: 1px solid #333; border-radius: 8px;
    color: #fff; padding: 6px 10px; font-size: 0.82rem;
  }
  .btn {
    width: 100%; padding: 13px; background: #e63946; color: #fff;
    border: none; border-radius: 10px; font-size: 1rem; font-weight: 600;
    cursor: pointer; margin-top: 16px; transition: background 0.2s;
  }
  .btn:hover { background: #c1121f; }
  .btn:disabled { background: #444; cursor: not-allowed; }
  #status { font-size: 0.85rem; color: #888; text-align: center; min-height: 20px; margin-top: 10px; }
  #result { display: none; }
  #result h2 { font-size: 1rem; margin-bottom: 10px; color: #fff; }
  #md-out {
    white-space: pre-wrap; font-family: 'Courier New', monospace; font-size: 0.78rem;
    line-height: 1.5; background: #111; padding: 14px; border-radius: 8px;
    max-height: 55vh; overflow-y: auto; border: 1px solid #2a2a2a; color: #d4d4d4;
  }
  .action-btn {
    flex: 1; min-width: 100px; padding: 9px; background: #2a2a2a; color: #ccc;
    border: 1px solid #444; border-radius: 8px; font-size: 0.82rem;
    cursor: pointer; text-align: center; text-decoration: none; display: block;
  }
  .action-btn:hover { background: #333; }
  .action-btn.green { background: #1a3a2a; color: #4caf80; border-color: #2a5a3a; }
  .action-btn.blue { background: #1a2a3a; color: #4a9fdf; border-color: #2a4a5a; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 99px;
    font-size: 0.7rem; font-weight: 600; margin-left: 6px;
  }
  .pill-ok { background: #1a3a2a; color: #4caf80; }
  .pill-err { background: #3a1a1a; color: #e63946; }
  .section-title { font-size: 0.82rem; color: #888; font-weight: 600; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #chat-section { display: none; margin-top: 12px; }
  #clip-section { display: none; margin-top: 12px; }
  #chat-out {
    background: #111; border: 1px solid #2a2a2a; border-radius: 8px; padding: 12px;
    font-size: 0.82rem; line-height: 1.5; color: #d4d4d4; min-height: 60px;
    white-space: pre-wrap; margin-top: 8px; display: none;
  }
  #clip-out {
    background: #111; border: 1px solid #2a2a2a; border-radius: 8px; padding: 12px;
    font-size: 0.82rem; line-height: 1.5; color: #d4d4d4; margin-top: 8px; display: none;
    white-space: pre-wrap; max-height: 30vh; overflow-y: auto;
  }
  .history-item {
    border-bottom: 1px solid #222; padding: 10px 0; cursor: pointer;
  }
  .history-item:last-child { border-bottom: none; }
  .history-item:hover { background: #1f1f1f; border-radius: 6px; padding: 10px; margin: 0 -10px; }
  .hist-title { font-size: 0.88rem; color: #ddd; font-weight: 500; }
  .hist-meta { font-size: 0.72rem; color: #666; margin-top: 2px; }
  .hist-summary { font-size: 0.78rem; color: #999; margin-top: 4px; line-height: 1.4; }
  #history-section { display: none; }
  .toggle-history { background: none; border: none; color: #666; font-size: 0.8rem; cursor: pointer; padding: 0; }
  .toggle-history:hover { color: #888; }
  #hist-search { margin-bottom: 10px; }
  .playlist-badge { background: #2a1a3a; color: #9a6fc4; border: 1px solid #4a3a6a; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; margin-top: 6px; display: none; }
  .clip-input { width: 80px !important; display: inline-block !important; }
</style>
</head>
<body>
<h1>yt-transcriber</h1>
<p class="sub">YouTube → Markdown · Chat · Clipping · SRT · Histórico</p>

<div class="card">
  <label>URL do YouTube (vídeo, short ou playlist)</label>
  <input type="text" id="url" placeholder="https://youtube.com/watch?v=... ou /playlist?list=..." autocomplete="off" spellcheck="false" oninput="checkPlaylist(this.value)">
  <div class="playlist-badge" id="playlist-badge">🎵 Playlist detectada — todos os vídeos serão transcritos</div>

  <div class="row">
    <div>
      <label style="margin-bottom:4px">Qualidade</label>
      <select id="quality">
        <option value="low">Rápida (small)</option>
        <option value="high">Precisa (medium)</option>
      </select>
    </div>
    <div>
      <label style="margin-bottom:4px">Idioma resumo</label>
      <select id="lang">
        <option value="pt">Português</option>
        <option value="en">English</option>
      </select>
    </div>
    <div>
      <label style="margin-bottom:4px">Áudio</label>
      <select id="audio-lang">
        <option value="pt">PT (forçar)</option>
        <option value="auto">Auto-detect</option>
        <option value="en">EN (forçar)</option>
      </select>
    </div>
  </div>

  <div class="row">
    <label class="toggle"><input type="checkbox" id="timestamps"> Timestamps</label>
    <label class="toggle"><input type="checkbox" id="study"> Notas de estudo</label>
    <label class="toggle"><input type="checkbox" id="reels"> Reel candidates</label>
  </div>

  <button class="btn" id="btn" onclick="submit()">Transcrever</button>
  <div id="status"></div>
</div>

<div class="card" id="result">
  <div style="display:flex; gap:12px; align-items:flex-start; margin-bottom:12px">
    <img id="thumb" src="" alt="" style="width:100px; border-radius:6px; display:none; flex-shrink:0">
    <div style="flex:1">
      <h2 style="margin-bottom:4px">Resultado <span class="pill pill-ok" id="engine-badge"></span></h2>
      <div id="result-meta" style="font-size:0.75rem; color:#666; margin-top:2px"></div>
    </div>
  </div>
  <div id="md-out"></div>

  <div class="row" style="margin-top:10px">
    <button class="action-btn" onclick="copyMd()">Copiar MD</button>
    <a id="dl-md" class="action-btn green" href="#" download>↓ .md</a>
    <a id="dl-txt" class="action-btn" href="#" download>↓ .txt</a>
    <a id="dl-srt" class="action-btn" href="#" download>↓ .srt</a>
    <a id="dl-json" class="action-btn blue" href="#" download>↓ .json</a>
  </div>

  <!-- Chat -->
  <div id="chat-section">
    <div class="section-title" style="margin-top:16px">Chat sobre o conteúdo</div>
    <input type="text" id="chat-input" placeholder="Pergunta sobre o vídeo..." onkeydown="if(e.key==='Enter')sendChat()">
    <div class="row" style="margin-top:8px">
      <button class="action-btn" onclick="sendChat()" id="chat-btn">Perguntar</button>
    </div>
    <div id="chat-out"></div>
  </div>

  <!-- Clipping -->
  <div id="clip-section">
    <div class="section-title" style="margin-top:16px">Clip de segmento</div>
    <div class="row" style="align-items:center; gap:8px">
      <span style="font-size:0.82rem; color:#aaa">De</span>
      <input type="number" id="clip-start" class="clip-input" placeholder="0" min="0" style="width:80px">
      <span style="font-size:0.82rem; color:#aaa">até</span>
      <input type="number" id="clip-end" class="clip-input" placeholder="60" min="0" style="width:80px">
      <span style="font-size:0.82rem; color:#aaa">seg</span>
      <button class="action-btn" onclick="getClip()" style="flex:none; padding:9px 14px">Extrair</button>
    </div>
    <div id="clip-out"></div>
  </div>
</div>

<!-- Histórico -->
<div class="card">
  <div style="display:flex; justify-content:space-between; align-items:center">
    <span class="section-title" style="margin:0">Histórico</span>
    <button class="toggle-history" onclick="toggleHistory()">mostrar ▾</button>
  </div>
  <div id="history-section">
    <input type="text" id="hist-search" placeholder="Buscar no histórico..." oninput="loadHistory(this.value)" style="margin-top:10px">
    <div id="hist-list"></div>
  </div>
</div>

<script>
let pollInterval = null;
let currentJobId = null;
let currentMd = "";
let historyVisible = false;

function checkPlaylist(url) {
  const badge = document.getElementById('playlist-badge');
  badge.style.display = url.includes('/playlist?list=') ? 'block' : 'none';
}

async function submit() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  const btn = document.getElementById('btn');
  btn.disabled = true;
  document.getElementById('result').style.display = 'none';
  document.getElementById('chat-section').style.display = 'none';
  document.getElementById('clip-section').style.display = 'none';
  setStatus('Enviando...');

  try {
    const res = await fetch('/transcribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        urls: [url],
        quality: document.getElementById('quality').value,
        lang: document.getElementById('lang').value,
        audio_lang: document.getElementById('audio-lang').value,
        timestamps: document.getElementById('timestamps').checked,
        study: document.getElementById('study').checked,
        reels: document.getElementById('reels').checked,
      })
    });
    const data = await res.json();
    currentJobId = data.job_id;
    pollInterval = setInterval(poll, 2000);
  } catch(e) {
    setStatus('Erro ao conectar: ' + e.message);
    btn.disabled = false;
  }
}

async function poll() {
  if (!currentJobId) return;
  try {
    const res = await fetch('/status/' + currentJobId);
    const data = await res.json();
    const step = data.current_step || '';
    const map = {
      downloading:'Baixando áudio...', transcribing:'Transcrevendo...',
      summarizing:'Gerando resumo...', 'study notes':'Notas de estudo...',
      diarizing:'Identificando speakers...', done:'Concluído!',
      expanding:'Expandindo playlist...',
    };
    const label = Object.entries(map).find(([k]) => step.startsWith(k));
    setStatus(label ? label[1] : (step || 'Processando...'));
    if (data.status === 'done') {
      clearInterval(pollInterval);
      fetchResult();
    } else if (data.status === 'error') {
      clearInterval(pollInterval);
      setStatus('Erro: ' + data.error);
      document.getElementById('btn').disabled = false;
    }
  } catch(e) {}
}

async function fetchResult() {
  const res = await fetch('/result/' + currentJobId);
  const data = await res.json();
  currentMd = data.markdown;

  const engine = currentMd.includes('legendas YT') ? 'legendas YT' : 'Whisper';
  document.getElementById('engine-badge').textContent = engine;
  document.getElementById('md-out').textContent = currentMd;

  // Show thumbnail and metadata from first result
  const results = data.results || [];
  if (results.length > 0) {
    const r = results[0];
    const thumb = r.info?.thumbnail;
    if (thumb) {
      const img = document.getElementById('thumb');
      img.src = thumb;
      img.style.display = 'block';
    }
    const meta = [];
    if (r.reading_time_minutes) meta.push(`~${r.reading_time_minutes}min leitura`);
    if (r.detected_language) meta.push(`idioma detectado: ${r.detected_language}`);
    if (results.length > 1) meta.push(`${results.length} vídeos`);
    document.getElementById('result-meta').textContent = meta.join(' · ');
  }

  const base = '/result/' + currentJobId;
  document.getElementById('dl-md').href = base + '/download';
  document.getElementById('dl-txt').href = base + '/download/txt';
  document.getElementById('dl-srt').href = base + '/download/srt';
  document.getElementById('dl-json').href = base + '/download/json';

  document.getElementById('result').style.display = 'block';
  document.getElementById('chat-section').style.display = 'block';
  document.getElementById('clip-section').style.display = data.has_segments ? 'block' : 'none';
  document.getElementById('btn').disabled = false;
  setStatus('');
  if (historyVisible) loadHistory();
}

async function sendChat() {
  const q = document.getElementById('chat-input').value.trim();
  if (!q || !currentJobId) return;
  const btn = document.getElementById('chat-btn');
  btn.textContent = 'Pensando...';
  btn.disabled = true;
  document.getElementById('chat-out').style.display = 'block';
  document.getElementById('chat-out').textContent = '...';
  try {
    const res = await fetch('/chat/' + currentJobId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q}),
    });
    const data = await res.json();
    document.getElementById('chat-out').textContent = data.answer || '(sem resposta)';
  } catch(e) {
    document.getElementById('chat-out').textContent = 'Erro: ' + e.message;
  }
  btn.textContent = 'Perguntar';
  btn.disabled = false;
}

document.addEventListener('DOMContentLoaded', () => {
  const ci = document.getElementById('chat-input');
  if (ci) ci.addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
});

async function getClip() {
  const start = parseFloat(document.getElementById('clip-start').value) || 0;
  const end = parseFloat(document.getElementById('clip-end').value) || 60;
  if (!currentJobId) return;
  const out = document.getElementById('clip-out');
  out.style.display = 'block';
  out.textContent = 'Extraindo...';
  try {
    const res = await fetch('/clip/' + currentJobId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start, end}),
    });
    const data = await res.json();
    out.textContent = data.text || '(sem segmentos neste intervalo)';
  } catch(e) {
    out.textContent = 'Erro: ' + e.message;
  }
}

function copyMd() {
  navigator.clipboard.writeText(currentMd).then(() => {
    const btns = document.querySelectorAll('.action-btn');
    const copy = [...btns].find(b => b.textContent === 'Copiar MD');
    if (copy) {
      copy.textContent = 'Copiado!';
      setTimeout(() => copy.textContent = 'Copiar MD', 1500);
    }
  });
}

async function toggleHistory() {
  historyVisible = !historyVisible;
  const sec = document.getElementById('history-section');
  const btn = document.querySelector('.toggle-history');
  sec.style.display = historyVisible ? 'block' : 'none';
  btn.textContent = historyVisible ? 'ocultar ▴' : 'mostrar ▾';
  if (historyVisible) loadHistory();
}

async function loadHistory(q) {
  const url = '/history' + (q ? '?q=' + encodeURIComponent(q) : '');
  const res = await fetch(url);
  const items = await res.json();
  const list = document.getElementById('hist-list');
  if (!items.length) {
    list.innerHTML = '<p style="color:#555;font-size:0.8rem;padding:10px 0">Sem transcrições ainda.</p>';
    return;
  }
  list.innerHTML = items.map(h => `
    <div class="history-item">
      <div class="hist-title">${h.title || '(sem título)'}</div>
      <div class="hist-meta">${h.channel || ''} · ${h.created_at ? h.created_at.slice(0,10) : ''}</div>
      ${h.summary ? '<div class="hist-summary">' + h.summary.slice(0,120) + '...</div>' : ''}
    </div>
  `).join('');
}

function setStatus(msg) { document.getElementById('status').textContent = msg; }

document.getElementById('url').addEventListener('keydown', e => {
  if (e.key === 'Enter') submit();
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class TranscribeRequest(BaseModel):
    urls: list[str]
    quality: str = "low"
    lang: str = "pt"
    audio_lang: str = "pt"  # whisper language; "auto" = auto-detect
    timestamps: bool = False
    study: bool = False
    speakers: bool = False
    reels: bool = False


class JobStatus(BaseModel):
    job_id: str
    status: str
    total: int
    completed: int
    current_step: str = ""
    file_path: str = ""
    error: str = ""


class ChatRequest(BaseModel):
    question: str


class ClipRequest(BaseModel):
    start: float
    end: float


def _cleanup_old_jobs():
    cutoff = datetime.now() - timedelta(hours=24)
    expired = [jid for jid, j in jobs.items() if j.get("created_at", datetime.now()) < cutoff]
    for jid in expired:
        jobs.pop(jid, None)


def _run_job(job_id: str, req: TranscribeRequest, urls: list[str]):
    def on_progress(current, total, step, detail):
        jobs[job_id]["completed"] = current
        jobs[job_id]["current_step"] = f"{step}: {detail[:60]}"

    try:
        jobs[job_id]["status"] = "processing"

        # Expand playlists
        expanded = []
        for u in urls:
            if is_playlist_url(u):
                jobs[job_id]["current_step"] = "expanding: playlist"
                expanded.extend(expand_playlist(u))
            else:
                expanded.append(u)
        jobs[job_id]["total"] = len(expanded)

        audio_lang = None if req.audio_lang == "auto" else req.audio_lang
        output_path = process_urls(
            expanded, quality=req.quality, lang=req.lang,
            audio_lang=audio_lang,
            timestamps=req.timestamps, on_progress=on_progress,
            study=req.study, speakers=req.speakers, reels=req.reels,
        )

        # Load JSON for history + job state
        json_path = output_path.with_suffix(".json")
        results_json = {}
        has_segments = False
        if json_path.exists():
            results_json = json.loads(json_path.read_text(encoding="utf-8"))
            for r in results_json.get("results", []):
                if r.get("transcript", {}).get("segments"):
                    has_segments = True
                    break
            _save_history(job_id, results_json, str(output_path))

        jobs[job_id].update(
            status="done",
            completed=len(expanded),
            file_path=str(output_path),
            current_step="done",
            results_json=results_json,
            has_segments=has_segments,
        )
    except Exception as e:
        jobs[job_id].update(status="error", error=str(e))
        log.error(f"Job {job_id} falhou: {e}")


@app.get("/", response_class=HTMLResponse)
def index():
    return UI_HTML


@app.post("/transcribe", response_model=JobStatus)
def transcribe(req: TranscribeRequest):
    _cleanup_old_jobs()
    valid = [u for u in req.urls if validate_url(u)]
    if not valid:
        raise HTTPException(400, "Nenhuma URL valida")

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued", "total": len(valid), "completed": 0,
        "current_step": "queued", "file_path": "", "error": "",
        "created_at": datetime.now(), "results_json": {}, "has_segments": False,
    }
    threading.Thread(target=_run_job, args=(job_id, req, valid), daemon=True).start()
    return JobStatus(job_id=job_id, status="queued", total=len(valid), completed=0)


@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    return JobStatus(job_id=job_id, **{k: j[k] for k in
                     ["status", "total", "completed", "current_step", "file_path", "error"]})


@app.get("/result/{job_id}")
def result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    md_path = Path(j["file_path"])
    if not md_path.exists():
        raise HTTPException(500, "Arquivo nao encontrado")
    results_json = j.get("results_json", {})
    # Strip heavy segment data from API response to keep payload small
    slim_results = []
    for r in results_json.get("results", []):
        slim_results.append({
            "info": r.get("info", {}),
            "summary": r.get("summary", ""),
            "reading_time_minutes": r.get("reading_time_minutes", 0),
            "detected_language": r.get("detected_language", ""),
        })
    return {
        "markdown": md_path.read_text(encoding="utf-8"),
        "file_path": j["file_path"],
        "has_segments": j.get("has_segments", False),
        "results": slim_results,
    }


@app.get("/result/{job_id}/download")
def download_md(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    md_path = Path(j["file_path"])
    if not md_path.exists():
        raise HTTPException(500, "Arquivo nao encontrado")
    return FileResponse(md_path, media_type="text/markdown", filename=md_path.name)


@app.get("/result/{job_id}/download/txt")
def download_txt(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    results = j.get("results_json", {}).get("results", [])
    lines = []
    for r in results:
        info = r.get("info", {})
        lines.append(info.get("title", ""))
        lines.append(info.get("url", ""))
        lines.append("")
        lines.append(r.get("summary", ""))
        lines.append("")
        lines.append(r.get("transcript", {}).get("text", ""))
        lines.append("\n---\n")
    txt = "\n".join(lines)
    md_path = Path(j["file_path"])
    return PlainTextResponse(txt, headers={
        "Content-Disposition": f'attachment; filename="{md_path.stem}.txt"'
    })


@app.get("/result/{job_id}/download/srt")
def download_srt(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    results = j.get("results_json", {}).get("results", [])
    all_segs = []
    offset = 0.0
    for r in results:
        segs = r.get("transcript", {}).get("segments", [])
        for seg in segs:
            all_segs.append({
                "start": seg["start"] + offset,
                "end": seg["end"] + offset,
                "text": seg["text"],
            })
        if segs:
            offset += segs[-1]["end"] + 1.0
    if not all_segs:
        raise HTTPException(400, "Sem segmentos com timestamp para SRT. Use Whisper (nao legendas YT sem timestamps).")
    srt = format_srt(all_segs)
    md_path = Path(j["file_path"])
    return PlainTextResponse(srt, headers={
        "Content-Disposition": f'attachment; filename="{md_path.stem}.srt"',
        "Content-Type": "text/plain; charset=utf-8",
    })


@app.get("/result/{job_id}/download/json")
def download_json(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    json_path = Path(j["file_path"]).with_suffix(".json")
    if not json_path.exists():
        raise HTTPException(500, "JSON nao encontrado")
    return FileResponse(json_path, media_type="application/json", filename=json_path.name)


@app.post("/chat/{job_id}")
def chat(job_id: str, req: ChatRequest):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, "Job ainda nao concluido")
    results = j.get("results_json", {}).get("results", [])
    if not results:
        raise HTTPException(400, "Sem transcrição disponível")

    # Concatenate transcripts (truncate for Haiku context)
    transcript_text = "\n\n".join(
        f"[{r['info'].get('title', '')}]\n{r['transcript'].get('text', '')}"
        for r in results
    )[:10000]

    lang_hint = "Responda em português." if True else "Answer in English."
    prompt = (
        f"Você é um assistente analisando a transcrição de um vídeo do YouTube.\n"
        f"{lang_hint}\n\n"
        f"TRANSCRIÇÃO:\n{transcript_text}\n\n"
        f"PERGUNTA: {req.question.strip()}"
    )
    answer = _haiku(prompt, timeout=60)
    return {"answer": answer or "(Sem resposta — Claude CLI indisponível)"}


@app.post("/clip/{job_id}")
def clip(job_id: str, req: ClipRequest):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, "Job ainda nao concluido")
    results = j.get("results_json", {}).get("results", [])

    matching = []
    for r in results:
        for seg in r.get("transcript", {}).get("segments", []):
            if seg["end"] >= req.start and seg["start"] <= req.end:
                matching.append(seg["text"].strip())

    if not matching:
        return {"text": "", "count": 0}
    return {"text": " ".join(matching), "count": len(matching)}


@app.get("/history")
def history(q: str = ""):
    return _get_history(q)


@app.post("/cookies/upload")
async def upload_cookies(request):
    """Recebe conteúdo de cookies.txt (Netscape format) e salva no VPS."""
    from fastapi import Request
    body = await request.body()
    if not body:
        raise HTTPException(400, "Corpo vazio")
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_bytes(body)
    line_count = body.count(b"\n")
    log.info(f"Cookies atualizados: {len(body)} bytes, {line_count} linhas")
    return {"ok": True, "bytes": len(body), "lines": line_count}


@app.get("/cookies/status")
def cookies_status():
    exists = COOKIES_PATH.exists()
    if exists:
        stat = COOKIES_PATH.stat()
        age_hours = (datetime.now().timestamp() - stat.st_mtime) / 3600
        return {"exists": True, "age_hours": round(age_hours, 1), "size_kb": round(stat.st_size / 1024, 1)}
    return {"exists": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8855)
