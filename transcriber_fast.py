#!/usr/bin/env python3
"""
Transcriber Fast — faster-whisper wrapper
4-6x mais rapido que openai-whisper via subprocess.
"""

import json
import logging
import sys
from pathlib import Path

from faster_whisper import WhisperModel

log = logging.getLogger("yt_transcriber.whisper")

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"]

_MODEL_CACHE: dict = {}


def _get_model(model: str, device: str = "cpu", compute_type: str = "int8") -> WhisperModel:
    cache_key = (model, device, compute_type)
    if cache_key not in _MODEL_CACHE:
        log.info(f"Carregando modelo faster-whisper '{model}' ({device}/{compute_type})...")
        _MODEL_CACHE[cache_key] = WhisperModel(model, device=device, compute_type=compute_type)
        log.info("Modelo carregado e cacheado.")
    return _MODEL_CACHE[cache_key]


def transcribe(video_path: Path, model: str = "medium", language: str = "pt",
               output_dir: Path = None, force: bool = False,
               device: str = "cpu", compute_type: str = "int8") -> dict:
    video_path = Path(video_path).expanduser()
    if not video_path.exists():
        raise FileNotFoundError(f"Video nao encontrado: {video_path}")

    if output_dir is None:
        output_dir = video_path.parent
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_file = output_dir / "transcript.json"

    if transcript_file.exists() and not force:
        log.info(f"Usando transcricao existente: {transcript_file}")
        return json.loads(transcript_file.read_text())

    log.info(f"Transcrevendo {video_path.name} com faster-whisper modelo '{model}'...")

    whisper = _get_model(model, device=device, compute_type=compute_type)

    segments_raw, info = whisper.transcribe(
        str(video_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    segments_list = list(segments_raw)
    transcript = _normalize_transcript(segments_list, info, video_path)

    transcript_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
    log.info(
        f"Transcricao salva: {transcript_file} | "
        f"{len(transcript['segments'])} segmentos | "
        f"{transcript['duration']:.1f}s"
    )
    return transcript


def _normalize_transcript(segments_raw: list, info, video_path: Path) -> dict:
    segments = []
    full_text_parts = []

    for idx, seg in enumerate(segments_raw):
        words = []
        for w in (seg.words or []):
            words.append({
                "word": w.word.strip(),
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "probability": round(w.probability, 3),
            })
        seg_text = seg.text.strip()
        full_text_parts.append(seg_text)
        segments.append({
            "id": idx,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg_text,
            "words": words,
        })

    duration = segments[-1]["end"] if segments else 0

    return {
        "source": str(video_path),
        "language": getattr(info, "language", "pt"),
        "text": " ".join(full_text_parts),
        "duration": duration,
        "segments": segments,
        "engine": "faster-whisper",
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Transcrever video com faster-whisper")
    parser.add_argument("video", help="Caminho do video")
    parser.add_argument("--model", default="medium", choices=WHISPER_MODELS)
    parser.add_argument("--language", default="pt")
    parser.add_argument("--output-dir", help="Diretorio de saida")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--compute-type", default="int8",
                        choices=["int8", "int8_float16", "float16", "float32"])
    args = parser.parse_args()

    t = transcribe(
        Path(args.video),
        model=args.model,
        language=args.language,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        force=args.force,
        device=args.device,
        compute_type=args.compute_type,
    )
    print(f"\n{'='*60}")
    print(f"Engine: {t.get('engine', 'unknown')}")
    print(f"Texto ({t['duration']:.1f}s):")
    print(t["text"][:500] + ("..." if len(t["text"]) > 500 else ""))
    print(f"Segmentos: {len(t['segments'])}")
