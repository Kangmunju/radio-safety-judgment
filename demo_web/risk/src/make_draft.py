"""팀 DB의 무전 → 작업 후 위험성평가 (risk_draft 저장 + KRAS 양식 엑셀).

먼저 팀 DB가 있어야 한다 (DB 담당 스크립트):
    python db/build_db.py --with-stt --classify --with-risk-data

흐름
1. 팀 DB(db/hackathon.db)의 radio_message 를 msg_id 순서대로 읽는다 (cls 가 비어 있으면 CLS로 채움)
2. 설비: message_equipment → 없으면 팀 db_api.find_equipment 로 문장에서 찾음
3. 정비요청이면 문장에서 부품+작업(NER 사전 PART·TASK)을 짝지어 risk_template 을 찾고 risk_draft 에 저장
   - 설비가 안 들렸으면 바로 뒤 복창의 설비로 채우고 '확인 필요'로 표시
4. 같은 장면에서 뒤에 나온 같은 설비의 상태보고(LOTO 적용·정비 완료·LOTO 해제)를 작업 기록으로 모은다
5. KRAS 양식(risk/data/kras_template.xlsx)에 채워 risk/output/<무전파일>_위험성평가.xlsx 로 저장
   - 가능성·중대성·위험성은 담당자가 입력 (빈칸)
   - --records-in-body 를 주면 무전 기록을 본문 칸(현재조치·완료일·담당자)에도 채운 _기록반영.xlsx 를 함께 만든다
"""
import argparse
import csv
import datetime as dt
import math
import re
import sqlite3
import sys
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font

REPO = Path(__file__).resolve().parents[2]
RISK = REPO / "risk"
sys.path.insert(0, str(REPO / "cls" / "src"))
sys.path.insert(0, str(REPO / "db"))
from classifier import UtteranceClassifier  # noqa: E402  팀 CLS
import db_api  # noqa: E402  팀 DB 함수

NER_DICT = REPO / "ner_dictionary.csv"
TEMPLATE_XLSX = RISK / "data" / "kras_template.xlsx"
OUT = RISK / "output"
RADIO_ROLE = {"A": "운전실", "B": "현장 정비 작업자 1", "C": "현장 정비 작업자 2"}
STATE_LABEL = {"LOTO적용": "LOTO 적용", "LOTO해제": "LOTO 해제", "정비완료": "정비 완료", "작업완료": "작업 완료",
               "정지확인": "정지 확인", "작업허가종료": "작업허가 종료"}
LOTO_MEASURE = re.compile(r"LOTO|전원\s*차단|전원차단|정지 상태")


def ns(s):
    return re.sub(r"\s+", "", s or "")


# ───────── NER 사전으로 단어 찾기
def load_terms():
    terms = {"PART": [], "TASK": [], "STATE": []}
    with NER_DICT.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["label"] in terms:
                terms[r["label"]].append((ns(r["term"]), r["norm"]))
    return {k: sorted(v, key=lambda x: -len(x[0])) for k, v in terms.items()}


def spans(terms, text):
    """사전 단어가 나온 위치 [(시작, 끝, 정규값)] — 긴 단어 우선, 겹치면 앞의 것."""
    t, taken, out = ns(text), set(), []
    for term, norm in terms:
        for m in re.finditer(re.escape(term), t) if term else []:
            if not taken & set(range(m.start(), m.end())):
                taken |= set(range(m.start(), m.end()))
                out.append((m.start(), m.end(), norm))
    return sorted(out)


def pair_works(text, terms):
    """'롤러 점검하고 낙광 청소해 줘' → [('롤러','점검'), ('낙광','청소')]. 부품 뒤 가장 가까운 작업과 짝짓는다."""
    parts, tasks = spans(terms["PART"], text), spans(terms["TASK"], text)
    pairs = []
    for s, e, part in parts:
        task = next((n for ts, _, n in tasks if ts >= e), None)
        if (part, task) not in pairs:
            pairs.append((part, task))
    if not parts:
        pairs = [(None, n) for _, _, n in tasks]
    return pairs


def states(text, terms):
    out = []
    for _, _, n in spans(terms["STATE"], text):
        if n not in out:
            out.append(n)
    return out


# ───────── 1~4단계: 팀 DB 읽기 · 분류 · 초안
def read_messages(conn, scenarios):
    rows = [dict(zip(("msg_id", "scenario", "file", "radio", "text", "cls"), r)) for r in conn.execute(
        "SELECT msg_id, scenario_id, file_name, radio_id, stt_text, cls FROM radio_message ORDER BY msg_id")]
    if any(r["cls"] is None for r in rows):            # 분류가 안 된 DB면 CLS로 채운다 (build_db --classify 와 같은 방식)
        clf, history, scenario = UtteranceClassifier(), [], None
        for r in rows:
            if r["scenario"] != scenario:
                scenario, history = r["scenario"], []
            if r["cls"] is None:
                p = clf.predict(r["text"], history)
                r["cls"] = p["cls"]
                conn.execute("UPDATE radio_message SET cls=?, cls_score=?, cls_rule=? WHERE msg_id=?",
                             (p["cls"], p["score"], p["rule"], r["msg_id"]))
            history.append(r["cls"])
    for r in rows:
        linked = [e for (e,) in conn.execute("SELECT equipment_id FROM message_equipment WHERE msg_id=? AND equipment_id"
                                             " IS NOT NULL ORDER BY link_id", (r["msg_id"],))]
        r["equips"] = linked or [h["equipment_id"] for h in db_api.find_equipment(r["text"] or "", db=conn)]
    return [r for r in rows if not scenarios or r["scenario"] in scenarios]


def make_draft(conn, req, rows, terms):
    same = [r for r in rows if r["scenario"] == req["scenario"] and r["msg_id"] > req["msg_id"]]
    works = pair_works(req["text"], terms)
    equips, equip_note = req["equips"], ""
    if not equips:                         # 설비가 안 들렸으면 바로 뒤 복창의 설비로 채운다
        readback = next((r for r in same if r["cls"] == "복창" and r["equips"]
                         and set(pair_works(r["text"], terms)) & set(works)), None)
        if readback:
            equips = readback["equips"]
            equip_note = (f"설비: 정비요청 무전에서 설비를 못 찾아 복창({Path(readback['file']).stem})의 "
                          f"{equips[0]}로 채움 — 확인 필요")
        else:
            equip_note = "설비 미확인 — 확인 필요"
    equip = equips[0] if equips else None
    drafts = []
    for part, task in works:
        row = conn.execute("SELECT risk_id, work_name FROM risk_template WHERE part=? AND task=?", (part, task)).fetchone()
        drafts.append({"part": part, "task": task, "risk_id": row[0] if row else None,
                       "work": row[1] if row else f"{part or '부품 미확인'} {task or '작업 미확인'}"})
    records = []
    for r in same:
        if r["cls"] == "상태보고" and equip in r["equips"]:
            st = states(r["text"], terms)
            records.append({"label": ", ".join(STATE_LABEL.get(s, s) for s in st) or "상태보고", "norms": st,
                            "file": Path(r["file"]).stem, "text": r["text"], "radio": r["radio"]})
    return equip, equip_note, drafts, records


# ───────── 5단계: 엑셀
def n_lines(text, per_line):
    return sum(max(1, math.ceil(len(p) / per_line)) for p in (text or "").split("\n"))


def draft_items(conn, drafts, equip=None):
    steps, gaps = [], []
    if equip and all(d["risk_id"] is None for d in drafts):
        # 작업을 못 찾았어도 설비를 알면 그 설비 종류의 공통 항목(작업 준비·마무리)은 채운다
        etype = conn.execute("SELECT type FROM equipment WHERE equipment_id=?", (equip,)).fetchone()
        rid = conn.execute("SELECT risk_id FROM risk_template WHERE equipment_type=? ORDER BY risk_id",
                           (etype[0] if etype else None,)).fetchone()
        if rid:
            steps += conn.execute("SELECT task_step, hazard_class, hazard, countermeasure, likelihood, severity, risk_grade"
                                  " FROM risk_step WHERE risk_id=? AND task_step IN ('작업 준비','작업 마무리')"
                                  " ORDER BY step_no", (rid[0],)).fetchall()
            gaps.append("작업을 찾지 못해 설비 공통 항목(작업 준비·마무리)만 채움")
    for d in drafts:
        if d["risk_id"] is None:
            gaps.append(f"작업 미확인: '{d['work']}' — 등록된 템플릿 없음 (담당자가 직접 작성)")
            continue
        gap = conn.execute("SELECT gap_note FROM risk_template WHERE risk_id=?", (d["risk_id"],)).fetchone()[0]
        if gap:
            gaps.append(f"추가 검토: {d['work']} — {gap}")
        steps += conn.execute("SELECT task_step, hazard_class, hazard, countermeasure, likelihood, severity, risk_grade"
                              " FROM risk_step WHERE risk_id=? ORDER BY step_no", (d["risk_id"],)).fetchall()
    seen, pre, body, post = set(), [], [], []
    for s in steps:                        # 작업 준비·마무리는 작업이 여러 개여도 한 번만
        if s[0] in ("작업 준비", "작업 마무리"):
            if (s[0], s[2]) in seen:
                continue
            seen.add((s[0], s[2]))
        (pre if s[0] == "작업 준비" else post if s[0] == "작업 마무리" else body).append(s)
    return pre + body + post, gaps


def export_excel(conn, req, equip, equip_note, drafts, records, path, records_in_body=False):
    name = conn.execute("SELECT name FROM equipment WHERE equipment_id=?", (equip,)).fetchone()
    items, gaps = draft_items(conn, drafts, equip)
    loto = next((r for r in records if "LOTO적용" in r["norms"]), None)
    done = next((r for r in records if {"정비완료", "작업완료"} & set(r["norms"])), None)
    worker = next((r["radio"] for r in records), None)

    wb = openpyxl.load_workbook(TEMPLATE_XLSX)
    del wb["00000000"]
    ws = wb["위험성평가(KRAS)"]
    for m in list(ws.merged_cells.ranges):
        if m.min_row >= 6 or m.min_row == 3:
            ws.unmerge_cells(str(m))
    data_style = {c: copy(ws.cell(6, c)._style) for c in range(1, 13)}
    for r in range(6, ws.max_row + 1):    # 본문 비우기 + 원본 양식에 남은 등급표·분류표 조각 지우기
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(r, c)
            if c <= 12:
                cell.value = None
            elif r >= 7:
                cell.value = None
                cell.style = "Normal"

    ws.merge_cells("B3:J3")
    equip_text = f"{name[0]}({equip})" if name else "설비 미확인"
    ws["B3"] = f"{equip_text} 정비 — {', '.join(d['work'] for d in drafts)}"
    ws["B3"].alignment = Alignment(horizontal="left", vertical="center")
    ws["L3"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rec = [f"· 작업 요청  {Path(req['file']).stem}  \"{req['text']}\""] + \
          [f"· {r['label']}  {r['file']}  \"{r['text']}\"" for r in records]
    ws["A2"] = "작업 기록(무전)\n" + "\n".join(rec)
    ws["A2"].font = Font(name="맑은 고딕", size=9)
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 13 * (len(rec) + 1) + 6

    for i, s in enumerate(items):
        r = 6 + i
        task_step, hclass, hazard, measure, lik, sev, grade = s
        current = done_at = owner = ""
        if records_in_body:
            if loto and LOTO_MEASURE.search(measure or ""):
                current = f"{loto['label']}\n(무전 {loto['file']})"
            done_at = f"정비 완료 보고\n({done['file']})" if done else ""
            owner = f"{worker} ({RADIO_ROLE.get(worker, '')})" if worker else ""
        for c, v in enumerate([task_step, hclass, hazard, current, lik, sev, grade, measure, "", "", done_at, owner], 1):
            cell = ws.cell(r, c)
            cell._style = copy(data_style[c])
            cell.value = v or None
        ws.row_dimensions[r].height = max(30, 11 * max(n_lines(hazard, 11), n_lines(measure, 20), n_lines(current, 18)) + 6)
    start = 6                              # 같은 세부작업은 A칸 병합
    for i in range(1, len(items) + 1):
        if i == len(items) or items[i][0] != items[start - 6][0]:
            if 6 + i - 1 > start:
                ws.merge_cells(start_row=start, end_row=6 + i - 1, start_column=1, end_column=1)
            start = 6 + i

    notes = ["※ 가능성·중대성·위험성(등급), 개선 후 위험성, 개선 예정일은 담당자가 입력",
             "※ 위험요인·감소대책: 사용자 입력(예시) — 한국가스공사 위험성평가 표준모델 문장의 어투를 쓰고 설비·부품·작업 단어만 바꿈"]
    if records_in_body:
        notes.append("※ 현재안전보건조치·완료일·담당자: 무전 기록에서 자동 입력")
    notes += [f"※ {x}" for x in ([equip_note] if equip_note else []) + gaps]
    g = 6 + len(items)
    ws.cell(g, 1).value = "\n".join(notes)
    for c in range(1, 13):
        ws.cell(g, c)._style = copy(data_style[c])
    ws.cell(g, 1).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.merge_cells(start_row=g, end_row=g, start_column=1, end_column=12)
    ws.row_dimensions[g].height = 12 * len(notes) + 8
    if ws.max_row > g:
        ws.delete_rows(g + 1, ws.max_row - g)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(items)


def main(argv=None):
    ap = argparse.ArgumentParser(description="팀 DB의 무전 → 작업 후 위험성평가 엑셀")
    ap.add_argument("scenarios", nargs="*", help="장면 (예: S01 S02). 비우면 전부")
    ap.add_argument("--db", default=str(db_api.DEFAULT_DB), help="팀 DB 파일 (기본 db/hackathon.db)")
    ap.add_argument("--records-in-body", action="store_true", help="무전 기록을 본문 칸에도 채운 _기록반영.xlsx 도 함께")
    a = ap.parse_args(argv)

    db_path = Path(a.db)
    if not db_path.exists():
        sys.exit(f"팀 DB가 없습니다: {db_path}\n먼저 만드세요: python db/build_db.py --with-stt --classify --with-risk-data")
    conn = sqlite3.connect(db_path)
    if not conn.execute("SELECT COUNT(*) FROM risk_template").fetchone()[0]:
        sys.exit("팀 DB에 위험성평가 표가 비어 있습니다. --with-risk-data 를 붙여 DB를 다시 만드세요:\n"
                 "  python db/build_db.py --with-stt --classify --with-risk-data")
    terms = load_terms()
    rows = read_messages(conn, set(a.scenarios))
    reqs = [r for r in rows if r["cls"] == "정비요청"]
    print(f"팀 DB 무전 {len(rows)}건 → 정비요청 {len(reqs)}건\n")
    for req in reqs:
        equip, equip_note, drafts, records = make_draft(conn, req, rows, terms)
        stem = Path(req["file"]).stem
        path = OUT / f"{stem}_위험성평가.xlsx"
        n = export_excel(conn, req, equip, equip_note, drafts, records, path)
        if a.records_in_body:
            export_excel(conn, req, equip, equip_note, drafts, records, OUT / f"{stem}_위험성평가_기록반영.xlsx", True)
        conn.execute("DELETE FROM risk_draft WHERE msg_id=?", (req["msg_id"],))     # 다시 실행해도 초안이 겹치지 않게
        for d in drafts:
            conn.execute("INSERT INTO risk_draft (msg_id, risk_id, status, equipment_id, created_at, excel_path)"
                         " VALUES (?,?,?,?,?,?)", (req["msg_id"], d["risk_id"], "DRAFT", equip,
                                                   dt.datetime.now().isoformat(timespec="seconds"),
                                                   path.relative_to(REPO).as_posix()))
        warn = f"  ⚠ {equip_note}" if equip_note else ("  ⚠ 작업 미확인" if any(d["risk_id"] is None for d in drafts) else "")
        print(f"{stem}  {equip or '설비?'}  {[d['work'] for d in drafts]}  본문 {n}행  기록 {len(records)}건{warn}")
    conn.commit()
    print(f"\n엑셀 → {OUT.relative_to(REPO)}\\   초안 → 팀 DB risk_draft ({db_path.name})")


if __name__ == "__main__":
    main()
