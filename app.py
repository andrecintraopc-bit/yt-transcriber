#!/usr/bin/env python3
"""
yt-transcriber — Web app (FastAPI + UI mobile-friendly)
Porta padrão: 8855
"""

import logging
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from yt_transcribe import process_urls, validate_url, OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yt_transcriber.app")

app = FastAPI(title="yt-transcriber", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict = {}

# ---------------------------------------------------------------------------
# HTML UI (mobile-first, sem dependências externas)
# ---------------------------------------------------------------------------

UI_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>yt-transcriber</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #e8e8e8; padding: 16px; }
  h1 { font-size: 1.2rem; font-weight: 600; margin-bottom: 4px; color: #fff; }
  .sub { font-size: 0.78rem; color: #888; margin-bottom: 20px; }
  .card { background: #1a1a1a; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  label { font-size: 0.8rem; color: #aaa; display: block; margin-bottom: 6px; }
  input[type=text], textarea {
    width: 100%; background: #111; border: 1px solid #333; border-radius: 8px;
    color: #fff; padding: 10px 12px; font-size: 0.95rem; outline: none;
  }
  input[type=text]:focus, textarea:focus { border-color: #555; }
  .row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
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
    max-height: 60vh; overflow-y: auto; border: 1px solid #2a2a2a; color: #d4d4d4;
  }
  .copy-btn {
    width: 100%; padding: 10px; background: #2a2a2a; color: #ccc;
    border: 1px solid #444; border-radius: 8px; font-size: 0.88rem;
    cursor: pointer; margin-top: 10px;
  }
  .copy-btn:hover { background: #333; }
  .dl-btn {
    width: 100%; padding: 10px; background: #1a3a2a; color: #4caf80;
    border: 1px solid #2a5a3a; border-radius: 8px; font-size: 0.88rem;
    cursor: pointer; margin-top: 6px; text-decoration: none; display: block; text-align: center;
  }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 99px;
    font-size: 0.7rem; font-weight: 600; margin-left: 6px;
  }
  .pill-ok { background: #1a3a2a; color: #4caf80; }
  .pill-err { background: #3a1a1a; color: #e63946; }
</style>
</head>
<body>
<h1>yt-transcriber</h1>
<p class="sub">Cole a URL do YouTube → receba o Markdown</p>

<div class="card">
  <label>URL do YouTube</label>
  <input type="text" id="url" placeholder="https://youtube.com/watch?v=..." autocomplete="off" spellcheck="false">

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
  <h2>Resultado <span class="pill pill-ok" id="engine-badge"></span></h2>
  <div id="md-out"></div>
  <button class="copy-btn" onclick="copyMd()">Copiar Markdown</button>
  <a id="dl-link" class="dl-btn" href="#" download>Baixar .md</a>
</div>

<script>
let pollInterval = null;
let currentJobId = null;
let currentMd = "";

async function submit() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  const btn = document.getElementById('btn');
  btn.disabled = true;
  document.getElementById('result').style.display = 'none';
  setStatus('Enviando...');

  try {
    const res = await fetch('/transcribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        urls: [url],
        quality: document.getElementById('quality').value,
        lang: document.getElementById('lang').value,
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
    const map = {downloading:'Baixando áudio...', transcribing:'Transcrevendo...', summarizing:'Gerando resumo...', 'study notes':'Notas de estudo...', diarizing:'Identificando speakers...', done:'Concluído!'};
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
  const dlLink = document.getElementById('dl-link');
  dlLink.href = '/result/' + currentJobId + '/download';
  document.getElementById('result').style.display = 'block';
  document.getElementById('btn').disabled = false;
  setStatus('');
}

function copyMd() {
  navigator.clipboard.writeText(currentMd).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = 'Copiado!';
    setTimeout(() => btn.textContent = 'Copiar Markdown', 1500);
  });
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
        output_path = process_urls(
            urls, quality=req.quality, lang=req.lang,
            timestamps=req.timestamps, on_progress=on_progress,
            study=req.study, speakers=req.speakers, reels=req.reels,
        )
        jobs[job_id].update(status="done", completed=len(urls),
                            file_path=str(output_path), current_step="done")
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
        "created_at": datetime.now(),
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
    return {"markdown": md_path.read_text(encoding="utf-8"), "file_path": j["file_path"]}


@app.get("/result/{job_id}/download")
def download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job nao encontrado")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job status: {j['status']}")
    md_path = Path(j["file_path"])
    if not md_path.exists():
        raise HTTPException(500, "Arquivo nao encontrado")
    return FileResponse(md_path, media_type="text/markdown", filename=md_path.name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8855)
