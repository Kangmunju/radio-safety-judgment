"""judge.py 시험. 임시 DB를 만들어 대본(radio_script.md)의 예상 판정 101개와 비교한다.

예상 값은 db/expected_judgments.csv (대본의 '예상 판정' 열을 5단계로 옮긴 것).
실행:  python db/test_judge.py
"""
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db_api  # noqa: E402
import judge  # noqa: E402

# 대본에서는 S08_01_A("이번 컨베이어")를 확인 필요로 봤지만, 앞뒤 맥락상 2번이 맞다고 보정되므로 정상으로 기대한다.
KNOWN_MISS = set()


def main():
    tmp = Path(tempfile.mkdtemp()) / "judge.db"
    subprocess.run([sys.executable, str(HERE / "build_db.py"), "--out", str(tmp), "--with-stt", "--classify", "--with-llm-results"],
                   check=True, capture_output=True)
    db = str(tmp)
    expected = {r["file_name"]: r["level"] for r in csv.DictReader(open(HERE / "expected_judgments.csv", encoding="utf-8-sig"))}
    results = judge.judge_all(db=db)
    got = {m["file_name"]: lv for m, lv, _ in results}
    miss = {f for f in expected if got.get(f) != expected[f]}
    for f in sorted(miss):
        print(f"  다름  {f}  기대 {expected[f]} / 결과 {got.get(f)}")
    ok = len(expected) - len(miss)
    print(f"대본 예상 판정 {ok}/{len(expected)} 일치")
    failed = []
    if miss - KNOWN_MISS:
        failed.append(f"예상 밖의 불일치: {sorted(miss - KNOWN_MISS)}")

    unk = judge.judge_message({"msg_id": 0, "cls": "불명", "stt_text": "??", "equipment": None, "action": None}, None, lambda i: None)
    if unk[0] != "확인 필요":
        failed.append(f"불명은 확인 필요여야 함: {unk[0]}")

    saved = {r["file_name"]: r for r in db_api.list_messages(db=db)}
    if not all(r["judgment"] for r in saved.values()):
        failed.append("판정이 저장되지 않은 무전이 있음")
    if saved["S06_02_B.wav"]["ref_msg_id"] != saved["S06_01_A.wav"]["msg_id"]:
        failed.append("복창 판정에 원지시가 연결되지 않음")
    judge.judge_scene("S01", db=db)           # 다시 돌려도 판정이 겹쳐 쌓이지 않는다
    n = sum(1 for r in db_api.list_messages("S01", db=db))
    import sqlite3
    c = sqlite3.connect(db).execute("SELECT COUNT(*) FROM judgment WHERE msg_id IN (SELECT msg_id FROM radio_message WHERE scenario_id='S01')").fetchone()[0]
    if c != n:
        failed.append(f"다시 실행하면 판정이 중복됨 ({c} != {n})")
    if failed:
        print("실패:", failed)
        sys.exit(1)
    print("통과")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
