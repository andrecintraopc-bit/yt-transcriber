#!/usr/bin/env python3
"""
yt-transcriber — YouTube Transcriber
Download audio, transcreve com faster-whisper, resume com Claude Haiku, gera Markdown.

Features:
  - Legendas YouTube como fallback (pula Whisper se legenda existe)
  - Modo Notas de Estudo (--study)
  - Resumo cruzado de batch (2+ videos)
  - Candidatos a Reel: trechos de alta densidade (--reels)
  - Speaker diarization (--speakers, requer pyannote.audio)

Saida padrao: ~/yt-transcricoes/
Override: env YT_OUTPUT_DIR
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("yt_transcriber")

# ---------------------------------------------------------------------------
# Paths — configuráveis via env var, sem acoplamento a outros projetos
# ---------------------------------------------------------------------------

def _which_ytdlp() -> Path:
    found = shutil.which("yt-dlp")
    if found:
        return Path(found)
    fallback = Path.home() / ".openclaw/venv/bin/yt-dlp"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        "yt-dlp nao encontrado. Instale com: pip install yt-dlp"
    )

YT_DLP = _which_ytdlp()
OUTPUT_DIR = Path(os.environ.get("YT_OUTPUT_DIR", str(Path.home() / "yt-transcricoes")))
TEMP_DIR = Path(os.environ.get("YT_TEMP_DIR", str(Path(tempfile.gettempdir()) / "yt_transcriber")))

sys.path.insert(0, str(Path(__file__).parent))
from transcriber_fast import _get_model, _normalize_transcript
from db import init_db, save_history


# ---------------------------------------------------------------------------
# Validacao e metadados
# ---------------------------------------------------------------------------

def validate_url(url: str) -> bool:
    patterns = [
        r'(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(https?://)?youtu\.be/[\w-]+',
        r'(https?://)?(www\.)?youtube\.com/shorts/[\w-]+',
        r'(https?://)?(www\.)?youtube\.com/playlist\?list=[\w-]+',
    ]
    return any(re.match(p, url.strip()) for p in patterns)


def is_playlist_url(url: str) -> bool:
    return bool(re.search(r'youtube\.com/playlist\?list=', url.strip()))


def expand_playlist(url: str) -> list[str]:
    """Expande uma playlist para lista de URLs individuais via yt-dlp."""
    cmd = [str(YT_DLP), "--flat-playlist", "--print", "url", "--no-warnings", url.strip()]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        log.info(f"Playlist expandida: {len(urls)} videos")
        return urls if urls else [url]
    except Exception as e:
        log.warning(f"Expansao de playlist falhou: {e}")
        return [url]


def get_video_info(url: str) -> dict:
    cmd = [str(YT_DLP), "--dump-json", "--no-download", url.strip()]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp info falhou: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    video_id = data.get("id", "")
    return {
        "title": data.get("title", "Sem titulo"),
        "channel": data.get("channel", data.get("uploader", "Desconhecido")),
        "duration": data.get("duration", 0),
        "upload_date": data.get("upload_date", ""),
        "url": url.strip(),
        "video_id": video_id,
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg" if video_id else "",
        "chapters": data.get("chapters", []),
    }


def reading_time_minutes(text: str) -> int:
    """Estima tempo de leitura em minutos (250 palavras/min)."""
    words = len(text.split())
    return max(1, round(words / 250))


# ---------------------------------------------------------------------------
# Legendas YouTube como fallback
# ---------------------------------------------------------------------------

def try_youtube_subtitles(url: str, lang: str = "pt") -> dict | None:
    """Tenta baixar legendas do YouTube (manuais ou auto). Retorna transcript dict ou None."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    output_template = str(TEMP_DIR / "%(id)s")

    for sub_flag in ["--write-sub", "--write-auto-sub"]:
        cmd = [
            str(YT_DLP),
            sub_flag,
            "--sub-lang", lang,
            "--sub-format", "json3",
            "--skip-download",
            "-o", output_template,
            "--no-playlist",
            url.strip(),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            continue

        sub_files = list(TEMP_DIR.glob(f"*.{lang}*.json3"))
        if not sub_files:
            continue

        sub_file = max(sub_files, key=lambda f: f.stat().st_mtime)
        try:
            data = json.loads(sub_file.read_text(encoding="utf-8"))
            events = data.get("events", [])
            if not events:
                sub_file.unlink(missing_ok=True)
                continue

            segments = []
            full_text_parts = []
            for evt in events:
                if "segs" not in evt:
                    continue
                text = "".join(s.get("utf8", "") for s in evt["segs"]).strip()
                if not text or text == "\n":
                    continue
                start = evt.get("tStartMs", 0) / 1000.0
                dur = evt.get("dDurationMs", 0) / 1000.0
                segments.append({
                    "id": len(segments),
                    "start": round(start, 3),
                    "end": round(start + dur, 3),
                    "text": text,
                    "words": [],
                })
                full_text_parts.append(text)

            sub_file.unlink(missing_ok=True)

            if segments:
                log.info(f"  Legendas YouTube encontradas ({len(segments)} segmentos) — pulando Whisper")
                return {
                    "source": "youtube-subtitles",
                    "language": lang,
                    "text": " ".join(full_text_parts),
                    "duration": segments[-1]["end"] if segments else 0,
                    "segments": segments,
                    "engine": "youtube-subtitles",
                }
        except (json.JSONDecodeError, KeyError):
            sub_file.unlink(missing_ok=True)
            continue

    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_audio(url: str, output_dir: Path = None) -> Path:
    if output_dir is None:
        output_dir = TEMP_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / "%(id)s.%(ext)s")
    cmd = [
        str(YT_DLP),
        "-x",
        "--audio-format", "m4a",
        "--audio-quality", "0",
        "-o", output_template,
        "--no-playlist",
        url.strip(),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download falhou: {result.stderr[:200]}")

    files = list(output_dir.glob("*.m4a")) + list(output_dir.glob("*.opus")) + list(output_dir.glob("*.webm"))
    if not files:
        raise FileNotFoundError(f"Nenhum audio encontrado em {output_dir}")
    return max(files, key=lambda f: f.stat().st_mtime)


# ---------------------------------------------------------------------------
# Transcricao + Resumo (Claude Haiku via MAX CLI, R$0)
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path: Path, model: str = "small", language: str | None = "pt") -> dict:
    whisper = _get_model(model, device="cpu", compute_type="int8")
    # language=None → Whisper auto-detects; "auto" treated as None
    lang_arg = None if language in (None, "auto") else language
    segments_raw, info = whisper.transcribe(
        str(audio_path),
        language=lang_arg,
        word_timestamps=True,
        vad_filter=False,
    )
    result = _normalize_transcript(list(segments_raw), info, audio_path)
    if lang_arg is None:
        result["detected_language"] = info.language
        log.info(f"  Idioma detectado: {info.language}")
    return result


def _haiku(prompt: str, timeout: int = 90) -> str:
    """Chama Claude Haiku via MAX CLI (R$0)."""
    cmd = ["claude", "-p", "--model", "haiku", prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        log.warning(f"Haiku falhou: {e}")
    return ""


def generate_summary(text: str, lang: str = "pt") -> str:
    if lang == "en":
        prompt = (
            "Summarize this transcript as 4-6 numbered key points (one per line). "
            "Each point: concise, actionable, max 20 words. No intro text.\n\n"
            f"Transcript:\n{text[:8000]}"
        )
    else:
        prompt = (
            "Resuma esta transcricao em 4-6 pontos numerados (um por linha). "
            "Cada ponto: conciso, direto, max 20 palavras. Sem texto de introducao.\n\n"
            f"Transcricao:\n{text[:8000]}"
        )
    return _haiku(prompt) or "(Resumo indisponivel)"


# ---------------------------------------------------------------------------
# Modo Notas de Estudo
# ---------------------------------------------------------------------------

def generate_study_notes(text: str, title: str, lang: str = "pt") -> str:
    if lang == "en":
        prompt = (
            f"Analyze this transcript from the video \"{title}\" and generate study notes:\n\n"
            f"## Key Insights\n- 5-7 bullet points with the main takeaways\n\n"
            f"## Technical Glossary\n- List any technical terms mentioned with brief definitions\n\n"
            f"## Key Questions\n- 3-5 questions this video answers\n\n"
            f"Transcript:\n{text[:8000]}"
        )
    else:
        prompt = (
            f"Analise esta transcricao do video \"{title}\" e gere notas de estudo:\n\n"
            f"## Insights Principais\n- 5-7 bullet points com os principais aprendizados\n\n"
            f"## Glossario Tecnico\n- Liste termos tecnicos mencionados com definicoes breves\n\n"
            f"## Perguntas-Chave\n- 3-5 perguntas que este video responde\n\n"
            f"Transcricao:\n{text[:8000]}"
        )
    return _haiku(prompt, timeout=120) or "(Notas de estudo indisponiveis)"


# ---------------------------------------------------------------------------
# Resumo cruzado de batch
# ---------------------------------------------------------------------------

def generate_cross_summary(summaries: list[dict], lang: str = "pt") -> str:
    if len(summaries) < 2:
        return ""
    summaries_text = "\n\n".join(
        f"Video {i+1}: \"{s['title']}\"\nResumo: {s['summary']}"
        for i, s in enumerate(summaries)
    )
    if lang == "en":
        prompt = (
            f"Compare these {len(summaries)} video summaries and generate:\n\n"
            f"## Common Points\n- What do these videos agree on?\n\n"
            f"## Divergent Points\n- Where do they disagree or present different perspectives?\n\n"
            f"## Consolidated Conclusion\n- What is the overall takeaway from watching all of them?\n\n"
            f"Summaries:\n{summaries_text[:8000]}"
        )
    else:
        prompt = (
            f"Compare estes {len(summaries)} resumos de videos e gere:\n\n"
            f"## Pontos em Comum\n- O que estes videos concordam?\n\n"
            f"## Pontos Divergentes\n- Onde divergem ou apresentam perspectivas diferentes?\n\n"
            f"## Conclusao Consolidada\n- Qual o aprendizado geral de assistir todos?\n\n"
            f"Resumos:\n{summaries_text[:8000]}"
        )
    return _haiku(prompt, timeout=120) or ""


# ---------------------------------------------------------------------------
# Candidatos a Reel (trechos de alta densidade — 30-60s)
# ---------------------------------------------------------------------------

def extract_reel_candidates(transcript: dict, min_dur: int = 30, max_dur: int = 60) -> list[dict]:
    segments = transcript.get("segments", [])
    if not segments or len(segments) < 3:
        return []

    candidates = []
    i = 0
    while i < len(segments):
        start_seg = segments[i]
        text_parts = [start_seg["text"]]
        end_seg = start_seg

        j = i + 1
        while j < len(segments):
            candidate_end = segments[j]
            if candidate_end["end"] - start_seg["start"] > max_dur:
                break
            text_parts.append(candidate_end["text"])
            end_seg = candidate_end
            j += 1

        clip_dur = end_seg["end"] - start_seg["start"]
        if clip_dur >= min_dur:
            full_text = " ".join(text_parts)
            word_count = len(full_text.split())
            density = word_count / clip_dur if clip_dur > 0 else 0
            candidates.append({
                "start": round(start_seg["start"], 1),
                "end": round(end_seg["end"], 1),
                "duration": round(clip_dur, 1),
                "text_preview": full_text[:150],
                "word_density": round(density, 2),
                "word_count": word_count,
            })
        i = j if j > i + 1 else i + 1

    candidates.sort(key=lambda c: c["word_density"], reverse=True)
    return candidates[:5]


# ---------------------------------------------------------------------------
# Speaker diarization (opcional — requer pyannote.audio)
# ---------------------------------------------------------------------------

def diarize_audio(audio_path: Path) -> list[dict] | None:
    try:
        from pyannote.audio import Pipeline as PyannotePipeline
    except ImportError:
        log.warning("pyannote.audio nao instalado. pip install pyannote.audio torch torchaudio")
        return None
    try:
        pipeline = PyannotePipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=True,
        )
        diarization = pipeline(str(audio_path))
        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": round(turn.start, 3),
                "end": round(turn.end, 3),
                "speaker": speaker,
            })
        return segments
    except Exception as e:
        log.warning(f"Diarization falhou: {e}")
        return None


def merge_transcript_with_speakers(transcript: dict, diarization: list[dict]) -> dict:
    if not diarization:
        return transcript

    def find_speaker(seg_start, seg_end):
        best, best_overlap = "Speaker ?", 0
        for d in diarization:
            overlap = max(0, min(seg_end, d["end"]) - max(seg_start, d["start"]))
            if overlap > best_overlap:
                best_overlap, best = overlap, d["speaker"]
        return best

    speaker_map, counter = {}, 1
    for seg in transcript["segments"]:
        raw = find_speaker(seg["start"], seg["end"])
        if raw not in speaker_map:
            speaker_map[raw] = f"Speaker {counter}"
            counter += 1
        seg["speaker"] = speaker_map[raw]

    transcript["speakers"] = list(speaker_map.values())
    return transcript


# ---------------------------------------------------------------------------
# Formatacao Markdown
# ---------------------------------------------------------------------------

def _fmt_dur(seconds: int) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def _fmt_ts(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def _srt_ts(seconds: float) -> str:
    ms = int(round((seconds % 1) * 1000))
    s = int(seconds) % 60
    m = int(seconds) // 60 % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(segments: list) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(seg['start'])} --> {_srt_ts(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def _fmt_date(date_str: str) -> str:
    if len(date_str) == 8:
        return f"{date_str[6:8]}/{date_str[4:6]}/{date_str[:4]}"
    return date_str


def format_markdown(results: list, timestamps: bool = False,
                    study: bool = False, cross_summary: str = "",
                    reels: bool = False) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Transcricoes YouTube — {today}\n"]

    for idx, r in enumerate(results, 1):
        info = r["info"]
        engine = r["transcript"].get("engine", "faster-whisper")
        engine_badge = " *(legendas YT)*" if engine == "youtube-subtitles" else ""

        read_min = reading_time_minutes(r["transcript"].get("text", ""))
        detected_lang = r["transcript"].get("detected_language", "")
        lang_note = f" · idioma: {detected_lang}" if detected_lang else ""
        lines.append(f"## {idx}. {info['title']}{engine_badge}")
        lines.append(
            f"**Canal:** {info['channel']} | "
            f"**Duracao:** {_fmt_dur(info['duration'])} | "
            f"**Leitura:** ~{read_min}min{lang_note} | "
            f"**Data:** {_fmt_date(info.get('upload_date', ''))}"
        )
        lines.append(f"**Link:** {info['url']}\n")
        lines.append("### Resumo")
        lines.append(f"{r['summary']}\n")

        if study and r.get("study_notes"):
            lines.append("### Notas de Estudo")
            lines.append(f"{r['study_notes']}\n")

        if reels and r.get("reel_candidates"):
            lines.append("### Candidatos a Reel (alta densidade)")
            for ci, c in enumerate(r["reel_candidates"], 1):
                lines.append(
                    f"{ci}. **{_fmt_ts(c['start'])} → {_fmt_ts(c['end'])}** "
                    f"({c['duration']}s, {c['word_count']} palavras)"
                )
                lines.append(f"   > {c['text_preview']}...")
            lines.append("")

        lines.append("### Transcricao")
        has_speakers = "speakers" in r["transcript"]

        if timestamps:
            for seg in r["transcript"]["segments"]:
                ts = _fmt_ts(seg["start"])
                sp = f"**{seg['speaker']}:** " if has_speakers and "speaker" in seg else ""
                lines.append(f"{ts} {sp}{seg['text']}")
        else:
            if has_speakers:
                current = None
                for seg in r["transcript"]["segments"]:
                    sp = seg.get("speaker", "")
                    if sp != current:
                        current = sp
                        lines.append(f"\n**{sp}:**")
                    lines.append(seg["text"])
            else:
                lines.append(r["transcript"]["text"])

        lines.append("\n---\n")

    if cross_summary:
        lines.append("## Analise Cruzada")
        lines.append(f"{cross_summary}\n")
        lines.append("---\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Processamento batch
# ---------------------------------------------------------------------------

def process_urls(urls: list, quality: str = "low", lang: str = "pt",
                 audio_lang: str | None = "pt",
                 timestamps: bool = False, output_name: str = None,
                 on_progress: callable = None,
                 study: bool = False, speakers: bool = False,
                 reels: bool = False, job_id: str | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    model = "medium" if quality == "high" else "small"
    results = []

    for i, url in enumerate(urls):
        url = url.strip()
        if not url or not validate_url(url):
            log.warning(f"URL invalida, pulando: {url}")
            continue

        log.info(f"[{i+1}/{len(urls)}] Processando: {url}")
        if on_progress:
            on_progress(i, len(urls), "downloading", url)

        try:
            info = get_video_info(url)
            log.info(f"  Titulo: {info['title']} ({_fmt_dur(info['duration'])})")

            transcript = try_youtube_subtitles(url, lang="pt")

            if transcript is None:
                audio_path = download_audio(url)
                log.info(f"  Audio: {audio_path.name}")
                if on_progress:
                    on_progress(i, len(urls), "transcribing", url)
                transcript = transcribe_audio(audio_path, model=model, language=audio_lang)
                log.info(f"  Transcrito: {len(transcript['segments'])} segmentos")

                if speakers:
                    if on_progress:
                        on_progress(i, len(urls), "diarizing", url)
                    diar = diarize_audio(audio_path)
                    if diar:
                        transcript = merge_transcript_with_speakers(transcript, diar)
                        log.info(f"  Speakers: {', '.join(transcript.get('speakers', []))}")

                audio_path.unlink(missing_ok=True)
            else:
                log.info("  Usando legendas YouTube (Whisper pulado)")

            if on_progress:
                on_progress(i, len(urls), "summarizing", url)

            summary = generate_summary(transcript["text"], lang=lang)
            log.info("  Resumo gerado")

            entry = {"info": info, "transcript": transcript, "summary": summary}

            if study:
                if on_progress:
                    on_progress(i, len(urls), "study notes", url)
                entry["study_notes"] = generate_study_notes(
                    transcript["text"], info["title"], lang=lang
                )
                log.info("  Notas de estudo geradas")

            if reels:
                entry["reel_candidates"] = extract_reel_candidates(transcript)

            results.append(entry)

        except Exception as e:
            log.error(f"  ERRO: {e}")
            results.append({
                "info": {"title": f"ERRO: {url}", "channel": "-", "duration": 0,
                         "upload_date": "", "url": url},
                "transcript": {"text": f"Falha: {e}", "segments": []},
                "summary": f"Transcricao falhou: {e}",
            })

    if not results:
        raise RuntimeError("Nenhum video processado com sucesso")

    cross_summary = ""
    if len(results) >= 2:
        log.info("Gerando resumo cruzado...")
        if on_progress:
            on_progress(len(urls), len(urls), "cross-summary", "comparando videos")
        cross_data = [
            {"title": r["info"]["title"], "summary": r["summary"]}
            for r in results if "ERRO" not in r["info"]["title"]
        ]
        cross_summary = generate_cross_summary(cross_data, lang=lang)

    md = format_markdown(results, timestamps=timestamps, study=study,
                         cross_summary=cross_summary, reels=reels)

    if output_name:
        filename = f"{output_name}.md"
    else:
        filename = f"transcricoes_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.md"

    output_path = OUTPUT_DIR / filename
    output_path.write_text(md, encoding="utf-8")
    log.info(f"Markdown salvo: {output_path}")

    # Salva JSON estruturado (segmentos + metadados) para chat, clipping e SRT
    json_path = output_path.with_suffix(".json")
    json_data = {
        "created_at": datetime.now().isoformat(),
        "results": [
            {
                "info": r["info"],
                "summary": r.get("summary", ""),
                "study_notes": r.get("study_notes", ""),
                "reel_candidates": r.get("reel_candidates", []),
                "transcript": r["transcript"],
                "reading_time_minutes": reading_time_minutes(r["transcript"].get("text", "")),
                "detected_language": r["transcript"].get("detected_language", ""),
            }
            for r in results
        ],
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"JSON salvo: {json_path}")

    if not job_id:
        import uuid
        job_id = str(uuid.uuid4())
    n_inserted = save_history(job_id, json_data, str(output_path))
    log.info(f"DB: {n_inserted} linha(s) inserida(s) em history (job_id={job_id[:8]})")

    if on_progress:
        on_progress(len(urls), len(urls), "done", str(output_path))

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="yt-transcriber — Transcreve videos do YouTube e gera Markdown"
    )
    parser.add_argument("urls", nargs="*", help="URLs do YouTube")
    parser.add_argument("--file", "-f", help="Arquivo .txt com URLs (1 por linha)")
    parser.add_argument("--quality", choices=["low", "high"], default="low",
                        help="low=modelo small (rapido), high=medium (preciso)")
    parser.add_argument("--lang", choices=["pt", "en"], default="pt",
                        help="Idioma do resumo gerado")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="Incluir timestamps [HH:MM:SS] na transcricao")
    parser.add_argument("--output", "-o", help="Nome do arquivo de saida (sem .md)")
    parser.add_argument("--study", "-s", action="store_true",
                        help="Gerar notas de estudo (bullets, glossario, perguntas)")
    parser.add_argument("--speakers", action="store_true",
                        help="Identificar speakers (requer pyannote.audio)")
    parser.add_argument("--reels", "-r", action="store_true",
                        help="Listar trechos de alta densidade (30-60s) no Markdown")

    args = parser.parse_args()

    all_urls = list(args.urls) if args.urls else []
    if args.file:
        file_path = Path(args.file)
        if file_path.exists():
            all_urls.extend(file_path.read_text().strip().splitlines())
        else:
            print(f"ERRO: Arquivo nao encontrado: {args.file}")
            sys.exit(1)

    if not all_urls:
        parser.print_help()
        sys.exit(1)

    valid = [u for u in all_urls if validate_url(u)]
    invalid = [u for u in all_urls if not validate_url(u)]
    for u in invalid:
        print(f"AVISO: URL invalida, ignorando: {u}")

    if not valid:
        print("ERRO: Nenhuma URL valida fornecida")
        sys.exit(1)

    features = []
    if args.timestamps: features.append("timestamps")
    if args.study: features.append("notas de estudo")
    if args.speakers: features.append("speakers")
    if args.reels: features.append("reel candidates")

    print(f"\n{'='*60}")
    print(f"yt-transcriber")
    print(f"Videos: {len(valid)} | Qualidade: {args.quality} | Idioma: {args.lang}")
    print(f"Features: {', '.join(features) if features else 'basico'}")
    print(f"Saida: {OUTPUT_DIR}")
    print(f"{'='*60}\n")

    output = process_urls(
        valid,
        quality=args.quality,
        lang=args.lang,
        timestamps=args.timestamps,
        output_name=args.output,
        study=args.study,
        speakers=args.speakers,
        reels=args.reels,
    )

    print(f"\n{'='*60}")
    print(f"CONCLUIDO: {output}")
    print(f"{'='*60}")
