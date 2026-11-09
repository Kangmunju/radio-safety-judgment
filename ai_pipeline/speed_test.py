"""실시간 속도 측정: 무전을 한 건씩 (실시간처럼) Whisper → 교정 → CLS → 추출 → DB 저장 → 판정.

묶음(batch) 평균이 아니라 무전 1건이 끝난 뒤 판정까지 걸리는 시간을 잰다.
    RTF  = STT 처리 시간 ÷ 음성 길이  (1보다 작으면 말하는 속도보다 빠름)
    지연 = 무전이 끝난 뒤 판정이 나오기까지 (STT + 후처리)

실행:
    source ~/Documents/GitHub/study/jisu/bin/activate
    python speed_test.py                    # large-v3, 101건 전부
    python speed_test.py --scene S01        # 장면 하나만
    python speed_test.py --model openai/whisper-large-v3-turbo
결과: speed_results.csv (파일별) + 화면에 요약
"""
import argparse
import csv
import re
import statistics as st
import time
from pathlib import Path

import db
import judge
from llm_post import Extractor, clean_text, read_csv
from pipeline import new_state, save_and_judge
from stt import DEFAULT_MODEL, SAMPLE_RATE, STT, load_wav
from team_cls import cls_text, load_classifier

BASE = Path(__file__).resolve().parent
from team_cls import REC_DIR  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser(description="무전 1건씩 실시간 처리 속도 측정")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--scene", help="장면 하나만 (예: S01)")
    ap.add_argument("--warmup", type=int, default=2, help="측정 전에 버리는 변환 횟수 (처음 몇 번은 느림)")
    ap.add_argument("--out", default=str(BASE / "speed_results.csv"))
    a = ap.parse_args()

    files = sorted(r["file"] for r in read_csv(BASE / "recordings.csv"))
    if a.scene:
        files = [f for f in files if f.startswith(a.scene.upper())]

    t0 = time.perf_counter()
    stt = STT(a.model)
    t_stt = time.perf_counter() - t0
    t0 = time.perf_counter()
    clf, ext = load_classifier(), Extractor(use_llm=False)
    conn = db.connect(":memory:")
    db.init_db(conn)
    t_post = time.perf_counter() - t0
    print(f"모델 로딩: Whisper {t_stt:.1f}초 · 후처리(CLS·추출·DB) {t_post:.1f}초  (서버 켤 때 한 번만)")

    warm = load_wav(REC_DIR / files[0])
    for _ in range(a.warmup):
        stt.transcribe(warm)
    clean_text("워밍업")

    rows, scene, J, state = [], None, None, None
    for k, f in enumerate(files, 1):
        if f[:3] != scene:  # 장면이 바뀌면 설비 상태·교신 기록을 새로
            scene = f[:3]
            db.reset_state(conn, scene)
            J, state = judge.Judge(conn), new_state()
        radio = re.search(r"_([A-Z])\.wav$", f).group(1)
        audio = load_wav(REC_DIR / f)
        sec = len(audio) / SAMPLE_RATE

        s0 = time.perf_counter()
        raw = stt.transcribe(audio)
        s1 = time.perf_counter()
        text = clean_text(raw)
        p = clf.predict(cls_text(text), state["cls_hist"])
        e = ext.extract(radio, raw, p["cls"], state["history"])
        m = save_and_judge(conn, J, state, scene, f, radio, raw, p["cls"], e, p.get("score"), p.get("rule"))
        s2 = time.perf_counter()

        r = {"file": f, "audio_sec": round(sec, 2), "stt_sec": round(s1 - s0, 3), "post_ms": round((s2 - s1) * 1000, 2),
             "latency_sec": round(s2 - s0, 3), "rtf": round((s1 - s0) / sec, 3), "level": m["level"], "text": e["corrected_text"]}
        rows.append(r)
        print(f"[{k:3d}/{len(files)}] {f}  음성 {sec:4.1f}s  STT {r['stt_sec']:5.2f}s  후처리 {r['post_ms']:6.1f}ms  "
              f"→ 판정까지 {r['latency_sec']:5.2f}s  {m['level']}")

    with open(a.out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    lat = [r["latency_sec"] for r in rows]
    stt_t = [r["stt_sec"] for r in rows]
    post = [r["post_ms"] for r in rows]
    audio = sum(r["audio_sec"] for r in rows)
    print("\n" + "=" * 64)
    print(f"모델 {a.model} · {len(rows)}건 · 무전 1건씩 처리 (묶음 없음)")
    print(f"음성 길이   평균 {st.mean(r['audio_sec'] for r in rows):.2f}s")
    print(f"STT         평균 {st.mean(stt_t):.2f}s · 중앙 {st.median(stt_t):.2f}s · 95% {pct(stt_t, 95):.2f}s · 최대 {max(stt_t):.2f}s")
    print(f"후처리      평균 {st.mean(post):.1f}ms · 최대 {max(post):.1f}ms  (교정 → CLS → 추출 → DB 저장 → 판정)")
    print(f"판정까지    평균 {st.mean(lat):.2f}s · 중앙 {st.median(lat):.2f}s · 95% {pct(lat, 95):.2f}s · 최대 {max(lat):.2f}s")
    print(f"RTF         {sum(stt_t) / audio:.3f}  (1보다 작으면 말하는 속도보다 빠름 → 실시간 가능)")
    print(f"저장: {a.out}")


if __name__ == "__main__":
    main()
