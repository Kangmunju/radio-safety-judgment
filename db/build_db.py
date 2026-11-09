"""AI 복창 확인 시스템 DB 만들기 (SQLite)

schema.sql(표) + seed.sql(설비 기준 데이터)로 DB 파일을 새로 만듭니다.
사전·STT 결과·위험성 평가 샘플은 옵션으로 합칠 수 있습니다.
DB 파일은 매번 지우고 다시 만들기 때문에, 파일을 고친 뒤 다시 실행하면 됩니다.

사용 예
    python build_db.py                          # 틀 + 초기 데이터만
    python build_db.py --ner-aliases script     # + NER 사전의 대본 별칭
    python build_db.py --with-stt --with-risk-sample
    python build_db.py --with-stt --classify    # + 팀 분류기(cls/src)로 cls 칸 채우기
    python build_db.py --out ../build/hackathon.db
"""
import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def read_csv(path):
    # 엑셀에서 저장한 BOM 있는 파일도 읽을 수 있도록 utf-8-sig 사용
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def merge_ner_aliases(con, mode, allow_ambiguous):
    """NER 사전의 설비 별칭(EQUIP)을 equipment_alias에 합친다.

    mode: none 합치지 않음 / script 출처가 '대본'인 것만 / all STT 오인식까지 전부
    메모에 '모호'가 적힌 별칭은 기본으로 건너뛴다 (--allow-ambiguous로 포함).
    seed.sql에 이미 들어 있는 별칭은 '이미 있음'으로 세고 영향을 주지 않는다.
    """
    if mode == "none":
        return
    rows = [r for r in read_csv(ROOT / "ner_dictionary.csv") if r["label"] == "EQUIP"]
    added, exists, skipped = [], 0, []
    for r in rows:
        if mode == "script" and r["source"] != "대본":
            continue
        cur = con.execute("SELECT equipment_id FROM equipment_alias WHERE alias = ?", (r["term"],)).fetchone()
        if cur:
            exists += 1
            if cur[0] != r["norm"]:
                skipped.append((r["term"], f"이미 {cur[0]}로 등록됨 (사전은 {r['norm']})"))
            continue
        if "모호" in r["note"] and not allow_ambiguous:
            skipped.append((r["term"], "메모에 '모호'"))
            continue
        con.execute("INSERT INTO equipment_alias(equipment_id, alias) VALUES (?, ?)", (r["norm"], r["term"]))
        added.append(r["term"])
    print(f"[NER 별칭 합치기: {mode}] 새로 추가 {len(added)}개, 이미 있음 {exists}개, 건너뜀 {len(skipped)}개")
    for term, why in skipped:
        print(f"    건너뜀: {term} ({why})")


def load_stt(con):
    """stt/stt_results.csv를 radio_message에 넣는다. cls, action, addressee_id는 비워 둔다."""
    rows = read_csv(ROOT / "stt" / "stt_results.csv")
    for r in rows:
        con.execute(
            "INSERT INTO radio_message(scenario_id, file_name, radio_id, stt_text) VALUES (?, ?, ?, ?)",
            (r["scenario"], r["file"], r["radio"], r["hyp_post"]),
        )
    print(f"[STT 결과] {len(rows)}건을 radio_message에 넣음 (cls·action·addressee_id는 NULL)")


def classify_messages(con):
    """radio_message의 stt_text를 팀 분류기(cls/src)로 분류해 cls 칸을 채운다.

    분류기는 '아니, 5번.' 같은 이어받기를 위해 앞 발화들의 분류(history)를 받는다.
    같은 scenario_id 안에서는 msg_id 순서대로 history를 쌓고, 시나리오가 바뀌면 비운다.
    (cls/src/run.py 와 같은 방식)
    """
    sys.path.insert(0, str(ROOT / "cls" / "src"))
    try:
        from classifier import UtteranceClassifier
    except ImportError as e:
        print(f"[분류] 분류기를 불러오지 못해 건너뜀: {e}  (scikit-learn, joblib 필요)")
        return
    clf = UtteranceClassifier()
    rows = con.execute("SELECT msg_id, scenario_id, stt_text FROM radio_message ORDER BY msg_id").fetchall()
    history, scenario, count = [], None, Counter()
    for msg_id, sc, text in rows:
        if sc != scenario:
            scenario, history = sc, []
        p = clf.predict(text, history)
        history.append(p["cls"])
        count[p["cls"]] += 1
        con.execute("UPDATE radio_message SET cls = ? WHERE msg_id = ?", (p["cls"], msg_id))
    print(f"[분류] {len(rows)}건 분류: " + " / ".join(f"{k} {v}" for k, v in count.most_common()))


def main():
    ap = argparse.ArgumentParser(description="SQLite DB 만들기")
    ap.add_argument("--out", default=str(HERE / "hackathon.db"), help="만들 DB 파일 경로")
    ap.add_argument("--ner-aliases", choices=["none", "script", "all"], default="none",
                    help="NER 사전의 설비 별칭을 합칠 범위 (기본: 합치지 않음)")
    ap.add_argument("--allow-ambiguous", action="store_true", help="메모에 '모호'가 적힌 별칭도 합침")
    ap.add_argument("--with-stt", action="store_true", help="STT 결과를 radio_message에 넣음")
    ap.add_argument("--with-risk-sample", action="store_true", help="위험성 평가 샘플 양식을 넣음")
    ap.add_argument("--classify", action="store_true", help="팀 분류기로 cls 칸을 채움 (--with-stt 필요)")
    args = ap.parse_args()
    if args.classify and not args.with_stt:
        ap.error("--classify는 --with-stt와 함께 써야 합니다 (분류할 문장이 있어야 하므로)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    con = sqlite3.connect(out)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "seed.sql").read_text(encoding="utf-8"))
    if args.with_risk_sample:
        con.executescript((HERE / "risk_sample.sql").read_text(encoding="utf-8"))
        print("[위험성 평가 샘플] 양식 3개, 단계 5개를 넣음 (예시용)")
    merge_ner_aliases(con, args.ner_aliases, args.allow_ambiguous)
    if args.with_stt:
        load_stt(con)
        if args.classify:
            classify_messages(con)
    con.commit()

    problems = con.execute("PRAGMA foreign_key_check").fetchall()
    print("\n[표별 행 수]")
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall():
        n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"    {name:<20} {n:>4}")
    print("\n외래키 검사:", "문제 없음" if not problems else f"문제 {len(problems)}건 {problems[:3]}")
    print("만든 파일:", out)
    con.close()


if __name__ == "__main__":
    main()
