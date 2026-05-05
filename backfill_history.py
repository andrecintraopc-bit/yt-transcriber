"""Backfill: lê todos os JSONs existentes em ~/yt-transcricoes/ e popula history.db.

Idempotente: usa job_id derivado do nome do arquivo, evita duplicar inserts em runs sucessivos
checando título+url+md_path antes do INSERT.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import init_db, save_history, DB_PATH, OUTPUT_DIR


def already_inserted(con, title: str, url: str, md_path: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM history WHERE title=? AND url=? AND md_path=? LIMIT 1",
        (title, url, md_path),
    ).fetchone()
    return row is not None


def main():
    init_db()
    json_files = sorted(OUTPUT_DIR.glob("*.json"))
    if not json_files:
        print("Nenhum JSON encontrado em", OUTPUT_DIR)
        return

    inserted = 0
    skipped = 0
    failed = 0

    con = sqlite3.connect(str(DB_PATH))

    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"FAIL ler {jf.name}: {e}")
            failed += 1
            continue

        # md_path = mesmo basename trocando .json por .md
        md_path = str(jf.with_suffix(".md"))

        # Verifica se algum result desse arquivo já está inserido
        results = data.get("results", [])
        if not results:
            print(f"SKIP {jf.name}: sem 'results'")
            skipped += 1
            continue

        # job_id derivado do basename do arquivo (idempotente)
        job_id = f"backfill:{jf.stem}"

        # Pula se TODOS os results já foram inseridos
        all_present = all(
            already_inserted(
                con,
                r.get("info", {}).get("title", ""),
                r.get("info", {}).get("url", ""),
                md_path,
            )
            for r in results
        )
        if all_present:
            print(f"SKIP {jf.name}: já em history.db")
            skipped += 1
            continue

        n = save_history(job_id, data, md_path)
        inserted += n
        print(f"INSERT {jf.name}: +{n} linha(s)")

    con.close()
    print(f"\n=== BACKFILL DONE ===")
    print(f"Arquivos JSON processados: {len(json_files)}")
    print(f"Linhas inseridas         : {inserted}")
    print(f"Skipped (já existia)     : {skipped}")
    print(f"Failed (erro de leitura) : {failed}")


if __name__ == "__main__":
    main()
