import csv
from pathlib import Path

from stt import STT

BASE_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = BASE_DIR / "recordings"
OUTPUT_CSV = BASE_DIR / "whisper_results.csv"


def main():
    wav_files = sorted(
        path
        for path in RECORDINGS_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() == ".wav"
    )

    if not wav_files:
        raise SystemExit(f"WAV 파일을 찾지 못했습니다: {RECORDINGS_DIR}")

    # prompt를 끄면(None) "B, 3번"이 "삐 번"으로 뭉개져 설비 번호가 사라진다 → 기본 용어 프롬프트 사용
    # large-v3: turbo보다 느리지만 더 정확한 원본 모델 (처음 실행 때 약 3GB 내려받음)
    stt = STT(model_id="openai/whisper-large-v3")

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=["file", "text", "error"])
        writer.writeheader()

        for index, audio_path in enumerate(wav_files, start=1):
            filename = audio_path.relative_to(RECORDINGS_DIR).as_posix()
            row = {"file": filename, "text": "", "error": ""}

            try:
                row["text"] = stt.transcribe(audio_path)
            except (OSError, ValueError, EOFError) as exc:
                row["error"] = str(exc)

            writer.writerow(row)
            csv_file.flush()
            print(
                f"[{index}/{len(wav_files)}] {filename} | {row['error'] or row['text']}"
            )

    print(f"\n저장 완료: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
