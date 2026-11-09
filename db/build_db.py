"""AI 복창 확인 시스템 DB 만들기 (SQLite)

schema.sql(표) + seed.sql(설비 기준 데이터)로 DB 파일을 새로 만듭니다.
사전·STT 결과·위험성 평가 데이터는 옵션으로 합칠 수 있습니다.
DB 파일은 매번 지우고 다시 만들기 때문에, 파일을 고친 뒤 다시 실행하면 됩니다.

사용 예
    python build_db.py                          # 틀 + 초기 데이터만
    python build_db.py --ner-aliases script     # + NER 사전의 대본 별칭
    python build_db.py --with-risk-data         # + 위험성 평가 정답지 (양식 9개, 단계 48행)
    python build_db.py --with-stt --with-risk-data --with-risk-drafts   # + risk/output 의 엑셀을 초안으로 등록
    python build_db.py --with-stt --classify    # + 팀 분류기(cls/src)로 cls·확신도·규칙 채우기,
                                                #   받는 무전기(addressee_id)는 대답한 무전기로 채우기
    python build_db.py --out ../build/hackathon.db
"""
import argparse
import csv
import re
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
        con.execute("UPDATE radio_message SET cls = ?, cls_score = ?, cls_rule = ? WHERE msg_id = ?",
                    (p["cls"], p["score"], p["rule"], msg_id))
    print(f"[분류] {len(rows)}건 분류: " + " / ".join(f"{k} {v}" for k, v in count.most_common()))


ORDER_LABELS = ("정비요청", "가동지시", "정지지시")
REPLY_LABELS = ("복창", "기타")     # 되묻기("몇 번 컨베이어 말씀이십니까?")는 기타로 분류되므로 함께 본다


def fill_addressees(con):
    """지시를 받은 무전기(addressee_id)를 채운다: 지시 뒤에 처음 대답(복창·되묻기)한 무전기가 받는 사람.

    호출부호가 '창우야'처럼 이름으로 들려도(STT) 이름과 상관없이 쓸 수 있는 방식이다.
    같은 scenario_id 안에서 지시 다음에 나오는 첫 복창(또는 되묻기)의 radio_id를 쓴다. (cls가 채워져 있어야 함)
    '안에 사람 있습니다! 가동 금지!' 같은 끼어들기는 정지지시로 분류되어 대답으로 보지 않는다.
    """
    rows = con.execute("SELECT msg_id, scenario_id, radio_id, cls FROM radio_message ORDER BY msg_id").fetchall()
    filled = 0
    for i, (msg_id, sc, rid, cls) in enumerate(rows):
        if cls not in ORDER_LABELS:
            continue
        for _, sc2, rid2, cls2 in rows[i + 1:]:
            if sc2 != sc:
                break
            if cls2 in REPLY_LABELS and rid2 != rid:
                con.execute("UPDATE radio_message SET addressee_id = ? WHERE msg_id = ?", (rid2, msg_id))
                filled += 1
                break
    print(f"[받는 무전기] 지시 {filled}건에 대답한 무전기를 채움")


def _cell_after(ws, label):
    """'공정명 :' 처럼 이름표가 적힌 칸의 오른쪽에서 처음 나오는 값을 돌려준다."""
    for row in ws.iter_rows(min_row=1, max_row=6):
        cells = list(row)
        for i, c in enumerate(cells):
            if isinstance(c.value, str) and c.value.strip().startswith(label):
                for nxt in cells[i + 1:]:
                    if nxt.value not in (None, ""):
                        return str(nxt.value).strip()
    return None


def register_risk_drafts(con):
    """risk/output/*_위험성평가.xlsx 를 risk_draft 에 등록한다.

    파일 이름 앞부분(예: S01_01_A)으로 그 무전(radio_message)을 찾고, 엑셀 안의
    '공정명'(설비 ID와 작업 이름)과 '평가일시'를 읽는다. 작업이 2개면 초안도 2건
    (설비 + 작업별 양식)으로 넣되 같은 엑셀·PDF 파일을 가리킨다. (ERD의 '1 : 0..N')
    같은 이름의 PDF가 옆에 있으면 pdf_path 에 함께 적는다.
    """
    try:
        import openpyxl
    except ImportError:
        print("[위험성 평가 초안] openpyxl 이 없어 건너뜀 (pip install openpyxl)")
        return
    out_dir = ROOT / "risk" / "output"
    files = sorted(out_dir.glob("*_위험성평가.xlsx")) if out_dir.exists() else []
    if not files:
        print("[위험성 평가 초안] risk/output 에 엑셀이 없어 건너뜀")
        return
    templates = {name: rid for rid, name in con.execute("SELECT risk_id, work_name FROM risk_template")}
    equipment = {r[0] for r in con.execute("SELECT equipment_id FROM equipment")}
    added = 0
    for xlsx in files:
        stem = xlsx.name[: -len("_위험성평가.xlsx")]
        msg = con.execute("SELECT msg_id FROM radio_message WHERE file_name = ?", (stem + ".wav",)).fetchone()
        if not msg:
            print(f"    건너뜀: {xlsx.name} (radio_message 에 {stem}.wav 가 없음 — --with-stt 필요)")
            continue
        ws = openpyxl.load_workbook(xlsx, data_only=True).worksheets[0]
        process, when = _cell_after(ws, "공정명"), _cell_after(ws, "평가일시")
        m = re.search(r"\(([A-Z]{2}-\d{2})\)", process or "")
        eq = m.group(1) if m and m.group(1) in equipment else None
        tasks = [x.strip() for x in (process or "").split("—")[-1].split(",") if x.strip()]
        pdf = xlsx.with_suffix(".pdf")
        for task in tasks:
            if task not in templates:
                print(f"    건너뜀: {xlsx.name} 의 작업 '{task}' 는 risk_template 에 없음")
                continue
            con.execute(
                "INSERT INTO risk_draft(msg_id, risk_id, status, equipment_id, created_at, excel_path, pdf_path)"
                " VALUES (?, ?, 'DRAFT', ?, ?, ?, ?)",
                (msg[0], templates[task], eq, when,
                 xlsx.relative_to(ROOT).as_posix(), pdf.relative_to(ROOT).as_posix() if pdf.exists() else None),
            )
            added += 1
    print(f"[위험성 평가 초안] 엑셀 {len(files)}개에서 초안 {added}건을 등록")


def main():
    ap = argparse.ArgumentParser(description="SQLite DB 만들기")
    ap.add_argument("--out", default=str(HERE / "hackathon.db"), help="만들 DB 파일 경로")
    ap.add_argument("--ner-aliases", choices=["none", "script", "all"], default="none",
                    help="NER 사전의 설비 별칭을 합칠 범위 (기본: 합치지 않음)")
    ap.add_argument("--allow-ambiguous", action="store_true", help="메모에 '모호'가 적힌 별칭도 합침")
    ap.add_argument("--with-stt", action="store_true", help="STT 결과를 radio_message에 넣음")
    ap.add_argument("--with-risk-data", action="store_true", help="위험성 평가 정답지(risk_data.sql)를 넣음")
    ap.add_argument("--with-risk-drafts", action="store_true",
                    help="risk/output 의 위험성평가 엑셀을 risk_draft 에 등록 (--with-stt, --with-risk-data 필요)")
    ap.add_argument("--classify", action="store_true", help="팀 분류기로 cls 칸을 채움 (--with-stt 필요)")
    args = ap.parse_args()
    if args.with_risk_drafts and not (args.with_stt and args.with_risk_data):
        ap.error("--with-risk-drafts는 --with-stt, --with-risk-data와 함께 써야 합니다 (무전과 양식이 있어야 연결되므로)")
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
    if args.with_risk_data:
        con.executescript((HERE / "risk_data.sql").read_text(encoding="utf-8"))
        n_t = con.execute("SELECT COUNT(*) FROM risk_template").fetchone()[0]
        n_s = con.execute("SELECT COUNT(*) FROM risk_step").fetchone()[0]
        print(f"[위험성 평가 정답지] 양식 {n_t}개, 단계 {n_s}행을 넣음")
    merge_ner_aliases(con, args.ner_aliases, args.allow_ambiguous)
    if args.with_stt:
        load_stt(con)
        if args.classify:
            classify_messages(con)
            fill_addressees(con)
        if args.with_risk_drafts:
            register_risk_drafts(con)
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
