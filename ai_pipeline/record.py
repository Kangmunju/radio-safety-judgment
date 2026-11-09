"""무전 대본 녹음 도구.

radio_script.md의 대사를 한 줄씩 보여주고 엔터로 녹음을 시작·종료한다.
녹음은 recordings/ 아래에 대본의 파일 이름(예: S01_04_A.wav)으로 저장하고,
녹음한 목록은 recordings.csv에 자동으로 기록한다.

사용법:
    python record.py               # 아직 녹음하지 않은 문장부터 이어서 녹음
    python record.py --scene S01   # S01 장면만 녹음
    python record.py --scene S01 --all   # S01을 이미 녹음한 것까지 다시 녹음
    python record.py --list        # 대본 목록과 녹음 여부만 출력
"""
import argparse
import csv
import re
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCRIPT = BASE / "radio_script.md"
if not SCRIPT.exists():  # 원본 대본이 없으면 공개용 대본 (대사 표 형식이 같음)
    SCRIPT = BASE / "radio_script_public.md"
from team_cls import REC_DIR as OUT_DIR  # noqa: E402  녹음 폴더 (없으면 팀 저장소 recordings/)
CSV_PATH = BASE / "recordings.csv"
SAMPLE_RATE = 16000  # Whisper 입력 기준

# | 4 | S01_04_A.wav | A | "B, 3번 컨베이어 다시 가동해 줘." | ...
ROW = re.compile(r'^\|\s*(\d+)\s*\|\s*(S\d+_\d+_[A-Z])\.wav\s*\|\s*([A-Z])\s*\|\s*"?(.+?)"?\s*\|')


def load_script():
    lines = []
    for row in SCRIPT.read_text(encoding="utf-8").splitlines():
        m = ROW.match(row)
        if not m:
            continue
        no, stem, radio, text = m.groups()
        scene, order, _ = stem.split("_")
        lines.append({
            "no": int(no),
            "file": f"{stem}.wav",
            "scene": scene,
            "order": int(order),
            "radio": radio,
            "text": text,
        })
    return lines


def is_recorded(line):
    return (OUT_DIR / line["file"]).exists()


def write_csv(lines):
    # 대본 순서대로, 녹음 파일이 있는 줄만 기록 (utf-8-sig: 엑셀에서 한글이 깨지지 않게)
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["file", "scenario", "order", "radio", "text"])
        for line in lines:
            if is_recorded(line):
                w.writerow([line["file"], line["scene"], line["order"], line["radio"], line["text"]])


def record(sd, np):
    chunks = []

    def callback(indata, frames, time, status):
        chunks.append(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback):
        input("  ● 녹음 중... 끝나면 엔터 ")
    return np.concatenate(chunks) if chunks else np.zeros((0, 1), dtype="int16")


def save(path, audio):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())


def take(sd, np):
    """녹음 → 들어보기/다시 녹음 → 저장할 오디오 반환."""
    while True:
        audio = record(sd, np)
        sec = len(audio) / SAMPLE_RATE
        print(f"  {sec:.1f}초 녹음됨" + ("  (너무 짧습니다)" if sec < 0.5 else ""))
        while True:
            cmd = input("  엔터: 저장 / p: 들어보기 / r: 다시 녹음 > ").strip().lower()
            if cmd == "p":
                sd.play(audio, SAMPLE_RATE)
                sd.wait()
            elif cmd == "r":
                break
            elif cmd == "":
                return audio


def main():
    parser = argparse.ArgumentParser(description="무전 대본 녹음 도구")
    parser.add_argument("--scene", help="이 장면만 녹음 (예: S01)")
    parser.add_argument("--all", action="store_true", help="이미 녹음한 문장도 다시 녹음")
    parser.add_argument("--list", action="store_true", help="대본 목록만 출력")
    args = parser.parse_args()

    lines = load_script()
    targets = [l for l in lines if not args.scene or l["scene"] == args.scene.upper()]

    if args.list:
        for l in targets:
            mark = "✓" if is_recorded(l) else " "
            print(f"[{mark}] #{l['no']:>3} {l['file']:<14} {l['radio']}  {l['text']}")
        done = sum(is_recorded(l) for l in targets)
        print(f"\n{done}/{len(targets)}개 녹음됨")
        return

    import numpy as np
    import sounddevice as sd

    OUT_DIR.mkdir(exist_ok=True)
    todo = targets if args.all else [l for l in targets if not is_recorded(l)]
    if not todo:
        print("녹음할 문장이 없습니다.")
        return

    print(f"녹음할 문장 {len(todo)}개. 무전기 A=운전실, B·C=현장 작업자\n")
    for i, line in enumerate(todo, 1):
        print(f"── [{line['scene']} #{line['no']}] 무전기 {line['radio']}  ({i}/{len(todo)})")
        print(f"   \"{line['text']}\"")
        cmd = input("  엔터: 녹음 시작 / s: 건너뛰기 / q: 종료 > ").strip().lower()
        if cmd == "q":
            break
        if cmd == "s":
            continue
        save(OUT_DIR / line["file"], take(sd, np))
        write_csv(lines)
        print(f"  저장: recordings/{line['file']}\n")

    done = sum(is_recorded(l) for l in lines)
    print(f"전체 {done}/{len(lines)}개 녹음됨. 목록: {CSV_PATH.name}")


if __name__ == "__main__":
    main()
