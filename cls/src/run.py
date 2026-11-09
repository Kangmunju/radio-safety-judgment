"""STT 결과 파일을 읽어서 CLS 결과를 한 줄씩 출력한다.

실행 (저장소 맨 위 폴더에서):
    python cls/src/run.py
다른 열이나 파일로 돌릴 때:
    python cls/src/run.py --col hyp                       # STT 원본
    python cls/src/run.py --file 다른파일.csv --col 열이름
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from classifier import UtteranceClassifier
from common import ROOT


def main():
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "stt" / "stt_results.csv"))
    ap.add_argument("--col", default="hyp_post", help="분류할 문장 열 (hyp = STT 원본, hyp_post = 보정본)")
    a = ap.parse_args()

    with open(a.file, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    clf = UtteranceClassifier()
    history, scenario, count = [], None, Counter()
    for r in rows:
        if r.get("scenario") != scenario:          # 시나리오가 바뀌면 앞 대화 초기화
            scenario, history = r.get("scenario"), []
        p = clf.predict(r[a.col], history)
        history.append(p["cls"])
        count[p["cls"]] += 1
        print(f"{Path(r['file']).stem:<10} {p['cls']:<5} {r[a.col]}")
    print(f"\n총 {len(rows)}건 | " + " / ".join(f"{k} {v}" for k, v in count.most_common()))


if __name__ == "__main__":
    main()
