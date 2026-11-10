"""Whisper 디코딩 파라미터 실험"""

import argparse
import csv
import time
from pathlib import Path

import torch

from stt import SAMPLE_RATE, STT, load_wav

BASE_DIR = Path(__file__).resolve().parent.parent.parent
RECORDINGS_DIR = BASE_DIR / "recordings"
EXPERIMENT_DIR = BASE_DIR / "experiments"

BASELINE = {"num_beams": 1, "temperature": 0.0, "max_new_tokens": 128}

PRESETS = {
    "baseline": {},
    "beam5": {"num_beams": 5},
    "fallback": {
        "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
    },
    "no_speech": {
        "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    },
    "no_repeat": {"no_repeat_ngram_size": 3},
}


@torch.inference_mode()
def transcribe(stt, audio_path, params):
    """stt.py의 STT.transcribe와 같지만 params를 generate()에 넘긴다."""
    audio = load_wav(audio_path)
    inputs = stt.processor(
        [audio],
        sampling_rate=SAMPLE_RATE,
        return_tensors="pt",
        return_attention_mask=True,
    )
    kwargs = dict(
        language="ko",
        task="transcribe",
        attention_mask=inputs.attention_mask.to(stt.device),
        **params,
    )
    if stt.prompt_ids is not None:
        kwargs["prompt_ids"] = stt.prompt_ids
    ids = stt.model.generate(inputs.input_features.to(stt.device, stt.dtype), **kwargs)
    text = stt.processor.batch_decode(ids, skip_special_tokens=True)[0]
    if stt.prompt:
        text = text.split(stt.prompt, 1)[-1]
    return text.strip()


def main():
    p = argparse.ArgumentParser(description="Whisper 디코딩 파라미터 실험")
    p.add_argument("--preset", default="baseline", choices=PRESETS)
    p.add_argument("--limit", type=int, help="앞에서부터 N개만 (빠른 시험용)")
    args = p.parse_args()

    wav_files = sorted(
        path
        for path in RECORDINGS_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() == ".wav"
    )
    if not wav_files:
        raise SystemExit(f"WAV 파일을 찾지 못했습니다: {RECORDINGS_DIR}")
    if args.limit:
        wav_files = wav_files[: args.limit]

    params = {**BASELINE, **PRESETS[args.preset]}
    EXPERIMENT_DIR.mkdir(exist_ok=True)
    output_csv = EXPERIMENT_DIR / f"whisper_{args.preset}.csv"

    stt = STT(model_id="openai/whisper-large-v3")
    print(f"프리셋 {args.preset}: {params}")

    total_audio = total_infer = 0.0
    empty = 0
    with output_csv.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(
            csv_file, fieldnames=["file", "text", "error", "audio_sec", "infer_sec"]
        )
        writer.writeheader()

        for index, audio_path in enumerate(wav_files, start=1):
            filename = audio_path.relative_to(RECORDINGS_DIR).as_posix()
            row = {
                "file": filename,
                "text": "",
                "error": "",
                "audio_sec": "",
                "infer_sec": "",
            }

            t0 = time.perf_counter()
            try:
                row["text"] = transcribe(stt, audio_path, params)
                row["audio_sec"] = round(len(load_wav(audio_path)) / SAMPLE_RATE, 2)
            except Exception as exc:  # 파일 하나가 실패해도 실험은 계속
                row["error"] = f"{type(exc).__name__}: {exc}"
            row["infer_sec"] = round(time.perf_counter() - t0, 3)

            if not row["error"]:
                total_audio += row["audio_sec"]
                total_infer += row["infer_sec"]
                empty += not row["text"]

            writer.writerow(row)
            csv_file.flush()
            print(
                f"[{index}/{len(wav_files)}] {filename} ({row['infer_sec']}s) | {row['error'] or row['text']}"
            )

    print(f"\n저장 완료: {output_csv}")
    if total_audio:
        print(
            f"처리 시간: {total_infer:.1f}초 / 오디오 {total_audio:.1f}초 (RTF {total_infer / total_audio:.3f})"
        )
    if empty:
        print(f"⚠ 빈 결과 {empty}건 — no_speech 설정 때문에 말이 지워졌는지 확인하세요")


if __name__ == "__main__":
    main()
