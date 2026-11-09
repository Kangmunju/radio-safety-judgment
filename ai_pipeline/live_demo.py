"""실시간 무전 관제 시연: ① 현장 무전 ━━ ② DB 실시간 적재 ━━ ③ 보고서 초안 (세 화면을 선으로 이어서).

    무전 1건 수신 (녹음 재생)
      → 서버가 그 자리에서 처리: 교정 → CLS → 설비·행위 추출 → live.db 저장 → 판정 (+ 담당자 버튼)
      → 화면: 말풍선 · AI 판정 · 경보 · DB INSERT (HeidiSQL로 live.db를 열면 같은 행이 바로 보인다)
    교신 종료 = 마지막 무전 뒤 SILENCE_SEC초 동안 무전 없음 (또는 화면의 [📝 보고서] 버튼)
      → 위험 상황 보고서 초안 (reports/*.html) + 정비요청이 있으면 위험성평가 엑셀 (팀 make_draft.py)

실행:
    source ~/Documents/GitHub/study/jisu/bin/activate
    python live_demo.py                      # http://localhost:8765 (S01 → S06 → S12)
    python live_demo.py --scenes S01,S04     # 장면 고르기
    python live_demo.py --stt                # 녹음을 그 자리에서 Whisper로 변환 (느림, 기본은 llm_results.csv 결과 사용)
    python live_demo.py --fresh              # live.db 를 비우고 시작
    python live_demo.py --share              # 같은 와이파이의 다른 기기에서도 접속 (주소가 화면에 나옴)
"""
import argparse
import datetime as dt
import html
import json
import re
import sqlite3
import sys
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import anim
import db
from judge import LEVEL_ICON, Judge
from llm_post import EQUIPMENT, read_csv
from pipeline import SCENARIO_BUTTONS, new_state, process_message, process_precomputed

BASE = Path(__file__).resolve().parent
from team_cls import REC_DIR  # noqa: E402  녹음 폴더 (없으면 팀 저장소 recordings/)
LIVE_DB = BASE / "live.db"                 # HeidiSQL 로 열어 보는 DB (SQLite)
REPORT_DIR = BASE / "reports"
LLM_CSV = BASE / "llm_results.csv"
from team_cls import TEAM_REPO  # noqa: E402
# 팀 위험성평가 초안 코드 (make_draft.py + risk/data 엑셀 양식). 팀 저장소 것을 쓰고, 없으면 demo_web 복사본
RISK_SRC = next((p for p in (TEAM_REPO / "risk" / "src", BASE / "demo_web" / "risk" / "src")
                 if (p / "make_draft.py").exists() and (p.parent / "data" / "kras_template.xlsx").exists()),
                TEAM_REPO / "risk" / "src")
PUBLIC_MD = BASE / "radio_script_public.md"
SILENCE_SEC = 6                            # 마지막 무전 뒤 이만큼 조용하면 교신 종료로 본다 (시연용으로 짧게)
DEFAULT_SCENES = ["S01", "S06", "S12"]


# ── 데이터 ────────────────────────────────────────────

def wav_seconds(path):
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error):
        return 2.0


def load_scenes():
    scenes = {}
    for r in sorted(read_csv(BASE / "recordings.csv"), key=lambda r: r["file"]):
        radio = re.search(r"_([A-Z])\.wav$", r["file"]).group(1)
        scenes.setdefault(r["file"][:3], []).append(
            {"file": r["file"], "radio": radio, "sec": wav_seconds(REC_DIR / r["file"])})
    return scenes


def load_titles():
    titles = {}
    if PUBLIC_MD.exists():
        for line in PUBLIC_MD.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^##\s*(S\d+)\.\s*(.+)$", line)
            if m:
                titles[m.group(1)] = m.group(2).strip()
    return titles


SCENES = load_scenes()
TITLES = load_titles()


# ── 실시간 처리 ───────────────────────────────────────

class Live:
    """한 번에 한 교신(장면)을 처리한다. 무전마다 live.db 에 바로 쓰고 commit 한다."""

    def __init__(self, use_stt=False, fresh=False):
        self.lock = threading.Lock()
        self.conn = db.connect(LIVE_DB)
        try:
            self.conn.execute("SELECT 1 FROM equipment LIMIT 1")
            empty = False
        except sqlite3.OperationalError:
            empty = True
        if fresh or empty:
            db.init_db(self.conn)
        self.use_stt = use_stt
        self.pre = {} if use_stt else {r["file"]: r for r in read_csv(LLM_CSV)}
        self.models = None
        self.run = None

    def _models(self):
        if self.models is None:
            from llm_post import Extractor
            from stt import STT
            from team_cls import load_classifier
            print("모델 불러오는 중 (Whisper · CLS · 추출)…", flush=True)
            self.models = (STT(), load_classifier(), Extractor(use_llm=False))
        return self.models

    def start(self, scene):
        with self.lock:
            db.reset_state(self.conn, scene)   # 설비 상태를 장면 시작 상태로 (무전·판정 기록은 남김)
            self.conn.commit()
            run_id = f"{scene}-{dt.datetime.now():%m%d-%H%M%S}"
            self.run = {"id": run_id, "scene": scene, "judge": Judge(self.conn), "state": new_state(),
                        "rows": SCENES[scene], "done": [], "msg_ids": []}
            return {"scene": scene, "run": run_id, "title": TITLES.get(scene, ""), "story": anim.STORY.get(scene, ""),
                    "initial": anim.board_state(self.conn),
                    "files": [{"file": r["file"], "radio": r["radio"], "sec": r["sec"], "audio": "/audio/" + r["file"]}
                              for r in SCENES[scene]]}

    def step(self, idx):
        with self.lock:
            run = self.run
            if run is None:
                raise ValueError("교신이 시작되지 않았습니다")
            while len(run["done"]) <= idx:        # 순서대로만 처리 (같은 무전을 두 번 넣지 않게)
                run["done"].append(self._process(run, run["rows"][len(run["done"])]))
            return run["done"][idx]

    def _process(self, run, row):
        conn, file, radio = self.conn, row["file"], row["radio"]
        if self.use_stt:
            stt, clf, ext = self._models()
            raw = stt.transcribe(str(REC_DIR / file))
            m = process_message(conn, clf, ext, run["judge"], run["state"], run["id"], file, radio, raw)
        else:
            m = process_precomputed(conn, run["judge"], run["state"], run["id"], file, radio, self.pre[file])
        run["msg_ids"].append(m["msg_id"])
        guess = [EQUIPMENT[e] for e in m["equipment_guess"]] if not m["equipment_ids"] else []
        msg = {"msg_id": m["msg_id"], "radio": radio, "to": m["addressee"], "text": m["corrected_text"],
               "cls": m["cls"], "equip": [EQUIPMENT[e] for e in m["equipment_ids"]], "guess": guess,
               "equip_ids": m["equipment_ids"] or m["equipment_guess"], "unknown": m["equipment_mentions"],
               "action": m["action"], "level": m["level"], "reason": anim.pretty(m["reason"]),
               "explain": anim.explain(m, conn), "board": anim.board_state(conn),
               "db": anim.inserted_rows(conn, m["msg_id"]), "llm": m.get("llm_check", "")}
        events = []
        for eid, act in SCENARIO_BUTTONS.get(file, []):   # 상태보고 뒤 담당자 버튼 (LOTO 적용 등)
            db.press_button(conn, eid, act, by="담당자", msg_id=m["msg_id"])
            log = anim._rows(conn, "SELECT log_id, equipment_id, action, msg_id, changed_by FROM state_change_log "
                                   "ORDER BY log_id DESC LIMIT 1")
            events.append({"text": f"담당자 버튼 · {EQUIPMENT[eid]} {db.BUTTON_LABEL[act]}", "equip_ids": [eid],
                           "board": anim.board_state(conn), "db": [{"table": "state_change_log", "row": r} for r in log]})
        conn.commit()                                     # HeidiSQL 에서 바로 보이게
        return {"msg": msg, "events": events}

    def report(self):
        with self.lock:
            if self.run is None or not self.run["msg_ids"]:
                raise ValueError("보고서로 만들 교신이 없습니다")
            return make_report(self.conn, self.run)


# ── 보고서 초안 ───────────────────────────────────────

ADVICE = {
    "충돌": "LOTO·작업허가가 걸린 설비에 가동 지시가 나왔습니다. 지시 전에 설비 상태(LOTO·작업허가)를 확인하는 절차를 "
            "운전실 지시 절차에 넣고, 해당 지시자·수신자 면담으로 경위를 확인하십시오.",
    "불일치": "복창이 원지시와 달랐습니다. 복창이 다르면 지시자가 즉시 정정하고 다시 복창받는 절차를 지키도록 교육하십시오.",
    "확인 필요": "설비를 특정할 수 없는 지시나 알아듣기 어려운 무전이 있었습니다. 설비 번호를 분명히 말하도록 교신 표준을 안내하십시오.",
}
LEVEL_COLOR = {"충돌": "#dc2626", "불일치": "#ea580c", "확인 필요": "#ca8a04", "정상": "#16a34a", "기록": "#6b7280"}


def risk_excels(conn, run):
    """정비요청 무전이 있으면 팀 위험성평가 초안(make_draft.py)으로 엑셀을 만들고 risk_draft 표에 남긴다."""
    if not RISK_SRC.exists():
        return [], ["demo_web/risk/src 가 없어 위험성평가 엑셀은 만들지 않았습니다"]
    if str(RISK_SRC) not in sys.path:
        sys.path.append(str(RISK_SRC))
    import make_draft as md
    files, notes = [], []
    msgs = md.read_messages(conn, {run["id"]})
    for req in (m for m in msgs if m["cls"] == "정비요청"):
        equip, note, drafts, records = md.make_draft(conn, req, msgs, md.load_terms())
        path = REPORT_DIR / f"{run['id']}_{Path(req['file']).stem}_위험성평가.xlsx"
        md.export_excel(conn, req, equip, note, drafts, records, path)
        for d in drafts:
            conn.execute("INSERT INTO risk_draft (msg_id, risk_id, status, equipment_id, created_at, excel_path)"
                         " VALUES (?,?,'DRAFT',?,datetime('now','localtime'),?)",
                         (req["msg_id"], d["risk_id"], equip, path.relative_to(BASE).as_posix()))
        files.append(path.name)
        if note:
            notes.append(note)
    return files, notes


def export_report_xlsx(path, run, info, headline, count, risky, logs, msgs, eq, advice, excels, who):
    """위험 상황 보고서를 엑셀로: [보고서] 시트 (개요·요약·위험 상황·설비 상태 변경·권고 조치·확인) + [교신 기록] 시트."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "보고서"
    thin = Side(style="thin", color="BFBFBF")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill, key_fill = PatternFill("solid", fgColor="DCE6F1"), PatternFill("solid", fgColor="F2F2F2")
    wrap = Alignment(wrap_text=True, vertical="top")
    fills = {lv: PatternFill("solid", fgColor=c) for lv, c in
             {"충돌": "F8D7DA", "불일치": "FDE2CC", "확인 필요": "FFF3CD", "정상": "D4EDDA", "기록": "EEEEEE"}.items()}
    for col, w in zip("ABCDEF", (12, 14, 46, 20, 12, 46)):
        ws.column_dimensions[col].width = w
    row = 1

    def title(text):
        nonlocal row
        row += 1
        ws.cell(row, 1, text).font = Font(bold=True, size=12, color="1F3864")
        row += 1

    def table(header, rows, level_col=None):
        nonlocal row
        for c, h in enumerate(header, 1):
            x = ws.cell(row, c, h)
            x.font, x.fill, x.border, x.alignment = Font(bold=True), head_fill, box, wrap
        row += 1
        for r in rows or [["없음"] + [""] * (len(header) - 1)]:
            for c, v in enumerate(r, 1):
                x = ws.cell(row, c, v)
                x.border, x.alignment = box, wrap
                if level_col is not None and c == level_col + 1 and r[level_col] in fills:
                    x.fill = fills[r[level_col]]
            row += 1

    ws.cell(row, 1, "무전 교신 위험 상황 보고서").font = Font(bold=True, size=16)
    ws.cell(row, 4, "AI 초안 · 담당자 검토 필요").font = Font(bold=True, color="9C5700")
    row += 1
    title("1. 개요")
    for k, v in info:
        a, b = ws.cell(row, 1, k), ws.cell(row, 2, v)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        a.font, a.fill, a.border, b.border, b.alignment = Font(bold=True), key_fill, box, box, wrap
        row += 1
    title("2. 요약")
    ws.cell(row, 1, headline).font = Font(bold=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    table(list(count), [[count[k] for k in count]])
    for c, k in enumerate(count, 1):
        ws.cell(row - 2, c).fill = fills[k]
    title("3. 탐지된 위험 상황")
    table(["시각", "무전", "내용", "설비", "판정", "판단 근거"],
          [[(m["created_at"] or "")[11:19], f"#{m['msg_id']} {who.get(m['radio_id'], m['radio_id'])}", m["stt_text"] or "",
            ", ".join(eq.get(m["msg_id"], [])) or "-", m["level"], anim.pretty(m["reason"] or "")] for m in risky], level_col=4)
    title("4. 설비 상태 변경 (담당자 버튼)")
    table(["시각", "설비", "변경", "근거 무전", "처리자"],
          [[(g["changed_at"] or "")[11:19], EQUIPMENT.get(g["equipment_id"], g["equipment_id"]),
            db.BUTTON_LABEL.get(g["action"], g["action"]), f"#{g['msg_id']}", g["changed_by"] or ""] for g in logs])
    title("5. 권고 조치 (초안)")
    for lv, text in advice or [("", "탐지된 위험 상황이 없어 별도 조치가 필요하지 않습니다.")]:
        ws.cell(row, 1, f"{lv} {count.get(lv, '')}건" if lv else "-").font = Font(bold=True)
        x = ws.cell(row, 2, text)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        x.alignment = wrap
        ws.row_dimensions[row].height = 32
        row += 1
    title("6. 첨부")
    for f in excels or ["정비요청 무전이 없어 위험성평가 초안 대상이 아닙니다."]:
        ws.cell(row, 1, f)
        row += 1
    title("7. 확인")
    table(["작성 (AI 초안)", "", "검토 (안전 담당)", "", "승인 (부서장)", ""], [["무전 안전 모니터링 시스템", "", "", "", "", ""]])
    ws.cell(row + 1, 1, "음성 인식(STT)과 규칙 기반 판정으로 자동 작성한 초안입니다. 원본 녹음과 함께 검토하십시오.").font = \
        Font(italic=True, color="7F7F7F", size=9)

    ws2 = wb.create_sheet("교신 기록")
    hdr = ["시각", "msg_id", "무전기", "받는 무전기", "분류", "행위", "내용 (STT 교정)", "설비", "판정", "판단 근거"]
    for c, (h, w) in enumerate(zip(hdr, (10, 8, 11, 11, 10, 8, 42, 20, 10, 46)), 1):
        x = ws2.cell(1, c, h)
        x.font, x.fill, x.border = Font(bold=True), head_fill, box
        ws2.column_dimensions[x.column_letter].width = w
    for r, m in enumerate(msgs, 2):
        vals = [(m["created_at"] or "")[11:19], m["msg_id"], who.get(m["radio_id"], m["radio_id"]),
                who.get(m["addressee_id"], m["addressee_id"] or ""), m["cls"] or "", m["action"] or "", m["stt_text"] or "",
                ", ".join(eq.get(m["msg_id"], [])), m["level"] or "", anim.pretty(m["reason"] or "")]
        for c, v in enumerate(vals, 1):
            x = ws2.cell(r, c, v)
            x.border, x.alignment = box, wrap
        if m["level"] in fills:
            ws2.cell(r, 9).fill = fills[m["level"]]
    ws2.freeze_panes = "A2"
    wb.save(path)


def make_report(conn, run):
    ids = run["msg_ids"]
    q = ",".join("?" * len(ids))
    msgs = [dict(r) for r in conn.execute(
        f"SELECT m.msg_id, m.file_name, m.radio_id, m.addressee_id, m.cls, m.action, m.stt_text, m.created_at, "
        f"j.level, j.reason, j.ref_msg_id FROM radio_message m LEFT JOIN judgment j USING (msg_id) "
        f"WHERE m.msg_id IN ({q}) ORDER BY m.msg_id", ids)]
    eq = {}
    for r in conn.execute(f"SELECT msg_id, equipment_id, mention_text FROM message_equipment WHERE msg_id IN ({q})", ids):
        name = EQUIPMENT.get(r["equipment_id"]) or f"{r['mention_text']}(DB에 없음)"
        if r["mention_text"] == "추정":
            name += "(추정)"
        eq.setdefault(r["msg_id"], []).append(name)
    logs = [dict(r) for r in conn.execute(
        f"SELECT equipment_id, action, msg_id, changed_at, changed_by FROM state_change_log WHERE msg_id IN ({q}) "
        f"ORDER BY log_id", ids)]
    REPORT_DIR.mkdir(exist_ok=True)
    try:
        excels, notes = risk_excels(conn, run)
    except Exception as e:  # noqa: BLE001  위험성평가가 안 돼도 보고서는 낸다
        excels, notes = [], [f"위험성평가 엑셀을 만들지 못했습니다: {e}"]
    conn.commit()
    risk_rows = [{"table": "risk_draft", "row": r} for r in anim._rows(
        conn, f"SELECT draft_id, msg_id, risk_id, status, equipment_id, excel_path FROM risk_draft WHERE msg_id IN ({q})", ids)]

    count = {lv: sum(1 for m in msgs if m["level"] == lv) for lv in LEVEL_ICON}
    risky = [m for m in msgs if m["level"] in ("충돌", "불일치", "확인 필요")]
    involved = sorted({n for v in eq.values() for n in v})
    radios = sorted({m["radio_id"] for m in msgs})
    t0, t1 = msgs[0]["created_at"] or "", msgs[-1]["created_at"] or ""
    now = dt.datetime.now()
    by_id = {m["msg_id"]: m for m in msgs}
    e = html.escape
    who = {"A": "운전실(A)", "B": "작업자 B", "C": "작업자 C"}

    if risky:
        headline = (f"교신 {len(msgs)}건 중 위험 상황 {len(risky)}건이 탐지되었습니다 "
                    f"(🔴 충돌 {count['충돌']} · 🟠 불일치 {count['불일치']} · 🟡 확인 필요 {count['확인 필요']}).")
    else:
        headline = f"교신 {len(msgs)}건에서 위험 상황이 탐지되지 않았습니다."

    def risk_row(m):
        ref = by_id.get(m["ref_msg_id"])
        ref_txt = f"<div class='ref'>비교한 지시 #{ref['msg_id']}: “{e(ref['stt_text'] or '')}”</div>" if ref else ""
        return (f"<tr><td>{e((m['created_at'] or '')[11:19])}</td><td>#{m['msg_id']}<br>{e(who.get(m['radio_id'], m['radio_id']))}</td>"
                f"<td>“{e(m['stt_text'] or '')}”{ref_txt}</td><td>{e(', '.join(eq.get(m['msg_id'], [])) or '-')}</td>"
                f"<td><b style='color:{LEVEL_COLOR[m['level']]}'>{LEVEL_ICON[m['level']]} {e(m['level'])}</b></td>"
                f"<td>{e(anim.pretty(m['reason'] or ''))}</td></tr>")

    def all_row(m):
        lv = m["level"] or ""
        return (f"<tr><td>{e((m['created_at'] or '')[11:19])}</td><td>#{m['msg_id']}</td><td>{e(who.get(m['radio_id'], m['radio_id']))}</td>"
                f"<td>{e(m['cls'] or '')}</td><td>“{e(m['stt_text'] or '')}”</td><td>{e(', '.join(eq.get(m['msg_id'], [])) or '-')}</td>"
                f"<td style='color:{LEVEL_COLOR.get(lv, '#000')}'>{LEVEL_ICON.get(lv, '')} {e(lv)}</td></tr>")

    log_rows = "".join(
        f"<tr><td>{e((g['changed_at'] or '')[11:19])}</td><td>{e(EQUIPMENT.get(g['equipment_id'], g['equipment_id']))}</td>"
        f"<td>{e(db.BUTTON_LABEL.get(g['action'], g['action']))}</td><td>#{g['msg_id']} “{e((by_id.get(g['msg_id']) or {}).get('stt_text') or '')}”</td>"
        f"<td>{e(g['changed_by'] or '')}</td></tr>" for g in logs) or "<tr><td colspan=5>없음</td></tr>"
    advice = "".join(f"<li><b>{LEVEL_ICON[lv]} {lv} {count[lv]}건</b> — {ADVICE[lv]}</li>" for lv in ADVICE if count[lv]) \
        or "<li>탐지된 위험 상황이 없어 별도 조치가 필요하지 않습니다.</li>"
    excel_html = "".join(f"<li><a href='/reports/{quote(f)}'>📥 {e(f)}</a></li>" for f in excels) + \
        "".join(f"<li class='muted'>{e(n)}</li>" for n in notes) or "<li class='muted'>정비요청 무전이 없어 위험성평가 초안 대상이 아닙니다.</li>"

    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>위험 상황 보고서 초안 {e(run['id'])}</title>
<style>
body{{font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;color:#111827;max-width:960px;margin:28px auto;padding:0 20px;line-height:1.5}}
h1{{font-size:24px;margin:0}} h2{{font-size:17px;border-left:5px solid #1d4ed8;padding-left:8px;margin:26px 0 8px}}
.draft{{display:inline-block;background:#fef3c7;color:#92400e;font-weight:800;font-size:12px;padding:3px 10px;border-radius:12px;margin-left:8px;vertical-align:middle}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}} th,td{{border:1px solid #d1d5db;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f3f4f6;white-space:nowrap}} .meta td:first-child{{background:#f9fafb;font-weight:700;width:150px}}
.head{{font-size:16px;font-weight:700;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px 14px}}
.cnt{{display:flex;gap:10px;margin-top:10px}} .cnt div{{flex:1;border:1px solid #e5e7eb;border-radius:8px;padding:8px;text-align:center;font-weight:800}}
.ref{{font-size:12px;color:#6b7280;margin-top:3px}} .muted{{color:#6b7280}} .sign td{{height:46px}}
@media print{{a{{color:#000}} .noprint{{display:none}}}}
</style></head><body>
<p class="noprint muted">브라우저 인쇄(Cmd+P) → PDF로 저장할 수 있습니다.</p>
<h1>무전 교신 위험 상황 보고서<span class="draft">AI 초안 · 담당자 검토 필요</span></h1>
<h2>1. 개요</h2>
<table class="meta">
<tr><td>보고서 번호</td><td>{e(run['id'])}</td></tr>
<tr><td>작성 일시</td><td>{now:%Y-%m-%d %H:%M} (교신 종료 후 자동 작성)</td></tr>
<tr><td>장소 · 채널</td><td>원료공장 · 무전 CH-1</td></tr>
<tr><td>교신 시간</td><td>{e(t0)} ~ {e(t1[11:])}</td></tr>
<tr><td>교신 참여</td><td>{e(', '.join(who.get(r, r) for r in radios))}</td></tr>
<tr><td>관련 설비</td><td>{e(', '.join(involved) or '-')}</td></tr>
<tr><td>근거 자료</td><td>live.db — radio_message · judgment · state_change_log (msg_id {ids[0]} ~ {ids[-1]})</td></tr>
</table>
<h2>2. 요약</h2>
<div class="head">{e(headline)}</div>
<div class="cnt">{''.join(f"<div style='color:{LEVEL_COLOR[lv]}'>{LEVEL_ICON[lv]} {lv}<br>{count[lv]}</div>" for lv in LEVEL_ICON)}</div>
<h2>3. 탐지된 위험 상황</h2>
<table><tr><th>시각</th><th>무전</th><th>내용</th><th>설비</th><th>판정</th><th>판단 근거</th></tr>
{''.join(risk_row(m) for m in risky) or "<tr><td colspan=6>없음</td></tr>"}</table>
<h2>4. 설비 상태 변경 (담당자 버튼)</h2>
<table><tr><th>시각</th><th>설비</th><th>변경</th><th>근거 무전</th><th>처리자</th></tr>{log_rows}</table>
<h2>5. 권고 조치 (초안)</h2><ul>{advice}</ul>
<h2>6. 첨부</h2><ul><li><a href='/reports/{quote(run["id"] + "_위험상황보고서.xlsx")}'>📊 이 보고서 엑셀</a></li>{excel_html}</ul>
<h2>7. 전체 교신 기록</h2>
<table><tr><th>시각</th><th>#</th><th>무전기</th><th>분류</th><th>내용 (STT 교정)</th><th>설비</th><th>판정</th></tr>
{''.join(all_row(m) for m in msgs)}</table>
<h2>8. 확인</h2>
<table class="sign"><tr><th>작성 (AI 초안)</th><th>검토 (안전 담당)</th><th>승인 (부서장)</th></tr><tr><td>무전 안전 모니터링 시스템</td><td></td><td></td></tr></table>
<p class="muted" style="font-size:12px;margin-top:16px">이 보고서는 무전 음성 인식(STT)과 규칙 기반 판정으로 자동 작성한 초안입니다. 음성 인식 오류가 있을 수 있으므로 원본 녹음과 함께 검토하십시오.</p>
</body></html>"""
    name = f"{run['id']}_위험상황보고서.html"
    (REPORT_DIR / name).write_text(body, encoding="utf-8")
    xname = f"{run['id']}_위험상황보고서.xlsx"
    info = [("보고서 번호", run["id"]), ("작성 일시", f"{now:%Y-%m-%d %H:%M} (교신 종료 후 자동 작성)"),
            ("장소 · 채널", "원료공장 · 무전 CH-1"), ("교신 시간", f"{t0} ~ {t1[11:]}"),
            ("교신 참여", ", ".join(who.get(r, r) for r in radios)), ("관련 설비", ", ".join(involved) or "-"),
            ("근거 자료", f"live.db — radio_message · judgment · state_change_log (msg_id {ids[0]} ~ {ids[-1]})")]
    export_report_xlsx(REPORT_DIR / xname, run, info, headline, count, risky, logs, msgs, eq,
                       [(lv, ADVICE[lv]) for lv in ADVICE if count[lv]], excels, who)
    top = [{"level": m["level"], "text": m["stt_text"], "reason": anim.pretty(m["reason"] or "")} for m in risky[:4]]
    return {"html": "/reports/" + quote(name), "name": name, "count": count, "total": len(msgs), "risky": top,
            "headline": headline, "excels": ["/reports/" + quote(f) for f in excels], "db": risk_rows,
            "xlsx": "/reports/" + quote(xname)}


# ── 화면 ──────────────────────────────────────────────
# ① 현장 무전 (캐릭터·녹음·판정 경보) ━━ ② DB 실시간 적재 (파이프라인 선 + 표) ━━ ③ 보고서 초안 (선 + 보고서)

def page(scenes):
    head, script = anim.TEMPLATE.split("<script>", 1)
    helpers = script[script.index("const LV ="):script.index("async function play(s)")]
    radios = sorted({r["radio"] for s in scenes for r in SCENES[s]} - {"A"}) or ["B"]
    workers = "".join(
        f"<div class='person worker' id='p{r}'><div class='bubble' id='b{r}'></div>"
        f"<div class='face flip'><img src='/char/worker' alt='작업자 {r}'></div>"
        f"<div class='label'>📻 작업자 {r}</div></div>" for r in radios)
    head = (head.replace("__WORKERS__", workers).replace("__CHAR__", "/char/bear")
            .replace('<button class="sub" id="restart">↺</button>',
                     '<button class="sub" id="rep" title="지금 교신을 끝내고 DB 적재·보고서로">📝 교신 마감</button>'
                     '<button class="sub" id="restart">↺</button>')
            .replace("<span>원료공장 · 무전 CH-1</span>", "<span>원료공장 · 무전 CH-1</span><span class='sep'>|</span>"
                     "<span style='opacity:.7'>DB: live.db</span>")
            .replace('<div class="grid">', STEPS_HTML + '<div class="grid" id="s1">')
            .replace('<div id="flash"></div>', SCREENS_HTML + '<div id="flash"></div>'))
    head = re.sub(r'\s*<div class="panel"><div class="ph"><span>📦.*?<div class="dblog" id="dblog"></div></div>', "",
                  head, count=1, flags=re.S)                       # ①에서는 DB 패널을 빼고 ②에서 크게 보여 준다
    head = head.replace("</head>", EXTRA_CSS + "</head>", 1)
    data = json.dumps([{"scene": s} for s in scenes], ensure_ascii=False)
    return head + "<script>\n" + helpers + LIVE_JS.replace("__SCENES__", data).replace("__SILENCE__", str(SILENCE_SEC)) \
        + "\n</script></body></html>"


STEPS_HTML = """<div class="steps">
  <div class="st" id="st1"><i>1</i>현장 무전</div><div class="ln"><b id="ln1"></b></div>
  <div class="st" id="st2"><i>2</i>DB 실시간 적재</div><div class="ln"><b id="ln2"></b></div>
  <div class="st" id="st3"><i>3</i>보고서 초안</div>
</div>
"""

SCREENS_HTML = """
<div class="scr" id="s2">
  <div class="panel pipe"><div class="ph"><span>⚙️ AI 처리 — 무전 한 건이 DB에 들어가기까지</span><span id="pmeta"></span></div>
    <div class="track" id="ptrack"></div></div>
  <div class="s2body">
    <div class="panel"><div class="ph"><span>🗄️ live.db — 실시간 적재 (팀 DB 구조)</span><span>HeidiSQL에서 새로고침하면 같은 행이 보입니다</span></div>
      <div class="tgrid" id="tgrid"></div></div>
    <div class="panel"><div class="ph"><span>⚖️ 판정 결과</span><span id="jcnt"></span></div><div class="jl" id="jl"></div></div>
  </div>
</div>
<div class="scr" id="s3">
  <div class="panel pipe"><div class="ph"><span>📝 교신 종료 → 보고서 초안</span><span id="rmeta"></span></div>
    <div class="track" id="rtrack"></div></div>
  <div class="s3body">
    <div class="panel doc"><iframe id="rframe" title="보고서 초안"></iframe><div class="wait" id="rwait">📝 보고서 작성 중<span class="dots"></span></div></div>
    <div class="panel rside" id="rside"></div>
  </div>
</div>
"""

EXTRA_CSS = """<style>
/* 진행 단계 선 */
.steps{display:flex;align-items:center;height:30px;padding:0 8px}
.st{display:flex;align-items:center;gap:8px;font-weight:800;font-size:13.5px;color:#64748b;white-space:nowrap;transition:color .4s}
.st i{width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:#1e293b;font-style:normal;font-size:12px;transition:all .4s}
.st.on{color:#f1f5f9}.st.on i{background:#2563eb;box-shadow:0 0 0 5px rgba(37,99,235,.25)}
.st.done{color:#86efac}.st.done i{background:#16a34a}
.ln{flex:1;height:4px;background:#1e293b;margin:0 14px;border-radius:2px;overflow:hidden}
.ln b{display:block;height:100%;width:0;background:linear-gradient(90deg,#2563eb,#22c55e);transition:width 1.2s ease}
/* ① 화면: DB 패널 대신 AI 판정·무전 기록을 오른쪽 전체로 */
.grid .right1{grid-column:2;grid-row:1 / span 2}
#s1{transition:opacity .5s}
/* ②③ 화면 */
.scr{position:absolute;left:12px;right:12px;bottom:10px;display:none;flex-direction:column;gap:10px;opacity:0;transition:opacity .6s}
.scr.show{display:flex}.scr.vis{opacity:1}
.pipe{flex:0 0 auto}
.track{position:relative;height:150px}
.rail{position:absolute;top:36px;height:4px;background:#1e293b;border-radius:2px}
.rail b{display:block;height:100%;width:0;background:linear-gradient(90deg,#60a5fa,#22c55e);transition:width .35s ease}
.node{position:absolute;top:6px;width:15.5%;transform:translateX(-50%);text-align:center;z-index:2}
.node .ic{width:60px;height:60px;margin:0 auto;border-radius:16px;background:#111827;border:2px solid #334155;display:flex;align-items:center;justify-content:center;font-size:28px;transition:all .25s}
.node.done .ic{border-color:#1d4ed8}
.node.on .ic{border-color:#60a5fa;background:#1e3a8a;box-shadow:0 0 20px rgba(96,165,250,.65);transform:scale(1.1)}
.node .nm{font-size:13px;font-weight:800;margin-top:6px}
.node .out{font-size:12.5px;margin-top:4px;color:#cbd5e1;line-height:1.3;max-height:50px;overflow:hidden}
.pkt{position:absolute;top:26px;width:24px;height:24px;border-radius:50%;background:#93c5fd;box-shadow:0 0 16px #60a5fa,0 0 4px #fff;transform:translateX(-50%);transition:left .35s ease;z-index:1;opacity:0}
.s2body{flex:1;display:grid;grid-template-columns:1.65fr 1fr;gap:10px;min-height:0}
.tgrid{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:8px;height:calc(100% - 24px)}
.tcard{background:#0b1220;border:1px solid #1e293b;border-radius:10px;overflow:hidden;display:flex;flex-direction:column;transition:border-color .3s}
.tcard.bump{border-color:#22c55e}
.tcard h4{margin:0;padding:6px 10px;font:800 12.5px ui-monospace,Menlo,monospace;color:#60a5fa;display:flex;justify-content:space-between;background:#0f172a}
.tcard h4 span{color:#94a3b8}
.tw{flex:1;overflow:hidden}
table.t{width:100%;border-collapse:collapse;font:11.5px ui-monospace,Menlo,monospace;table-layout:fixed}
.t th{color:#94a3b8;text-align:left;padding:3px 6px;border-bottom:1px solid #1e293b;font-weight:700}
.t td{padding:3px 6px;border-bottom:1px solid #111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t tr.new td{animation:newdb 1.6s}
.jl{height:calc(100% - 24px);overflow:hidden;font-size:13px}
.jl div{display:grid;grid-template-columns:40px 1fr auto;gap:8px;padding:6px 4px;border-bottom:1px solid #162033;align-items:center}
.jl div.new{animation:newrow 1.2s}
.jl .tx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s3body{flex:1;display:grid;grid-template-columns:1.7fr 1fr;gap:10px;min-height:0}
.doc{padding:0;background:#fff;position:relative}
.doc iframe{border:0;width:100%;height:100%;opacity:0;transition:opacity .6s}
.doc iframe.vis{opacity:1}
.doc .wait{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#334155;font-size:20px;font-weight:800}
.rside{display:flex;flex-direction:column;gap:10px;font-size:14px}
.rside .hd{font-size:17px;font-weight:900;line-height:1.4}
.rside .cn{display:flex;flex-wrap:wrap;gap:6px}
.rside .cn span{padding:4px 10px;border-radius:14px;background:#111827;border:1px solid #334155;font-weight:800;font-size:13px}
.rside .rk div{padding:6px 0;border-bottom:1px solid #1e293b;font-size:13px;line-height:1.4}
.rside a,.rside button{display:block;text-align:center;font-size:15px;font-weight:800;padding:10px;border-radius:10px;background:#16a34a;color:#fff;text-decoration:none;border:0;cursor:pointer}
.rside a.x{background:#0f766e}.rside button{background:#2563eb}
.rside .sp{flex:1}
.rside .h{font-size:12px;opacity:.6}
.silence{font-size:15px;margin-top:6px}
</style>"""


LIVE_JS = r"""
const SCENES = __SCENES__;
const SILENCE = __SILENCE__;      // 마지막 무전 뒤 이만큼(초) 조용하면 교신 종료
const NEXT_SEC = 15;              // ③ 보고서 화면 뒤 다음 장면까지
const P_NODES = [["📻", "무전 수신"], ["🎙️", "STT · 교정"], ["🏷️", "발화 분류 (CLS)"], ["🔎", "설비 · 행위 추출"], ["⚖️", "판정 (규칙)"], ["🗄️", "DB 적재"]];
const R_NODES = [["🗄️", "live.db"], ["🔎", "위험 상황 추리기"], ["📝", "보고서 작성"], ["📄", "보고서 초안"]];
const T_COLS = {radio_message: ["msg_id", "radio_id", "cls", "action", "stt_text"],
                message_equipment: ["link_id", "msg_id", "equipment_id", "mention_text"],
                judgment: ["judgment_id", "msg_id", "ref_msg_id", "level", "reason"],
                state_change_log: ["log_id", "equipment_id", "action", "msg_id", "changed_by"]};
const T_W = {stt_text: "40%", reason: "38%", mention_text: "30%", judgment_id: "17%", ref_msg_id: "15%", level: "12%",
             link_id: "13%", log_id: "11%", equipment_id: "20%", action: "16%"};
let si = 0, cur = null, gen = 0, results = [], endEarly = false;
const api = async p => { const r = await fetch(p); if (!r.ok) throw new Error(await r.text()); return r.json(); };
const radiosOf = c => [...new Set(c.files.map(f => f.radio))].sort();

// ── 화면 전환 (① ━━ ② ━━ ③) ──
function placeScreens() { const top = $("s1").offsetTop; document.querySelectorAll(".scr").forEach(s => s.style.top = top + "px"); }
async function toScreen(k, instant) {
  ["st1", "st2", "st3"].forEach((id, n) => { $(id).className = "st" + (n + 1 < k ? " done" : n + 1 === k ? " on" : ""); });
  $("ln1").style.width = k >= 2 ? "100%" : "0"; $("ln2").style.width = k >= 3 ? "100%" : "0";
  const show = {1: "s1", 2: "s2", 3: "s3"}[k];
  if (!instant) {
    document.querySelectorAll(".scr.vis").forEach(s => s.classList.remove("vis"));
    $("s1").style.opacity = 0; await sleep(600);
  }
  $("s1").style.visibility = k === 1 ? "visible" : "hidden"; $("s1").style.opacity = k === 1 ? 1 : 0;
  document.querySelectorAll(".scr").forEach(s => { s.classList.toggle("show", s.id === show); if (s.id !== show) s.classList.remove("vis"); });
  if (k > 1) { placeScreens(); void $(show).offsetWidth; $(show).classList.add("vis"); }
}

// ── 파이프라인 선 ──
function buildTrack(id, nodes, p) {
  const n = nodes.length;
  $(id).innerHTML = `<div class="rail" style="left:${50 / n}%;right:${50 / n}%"><b id="${p}fill"></b></div>` +
    nodes.map(([ic, nm], k) => `<div class="node" id="${p}${k}" style="left:${(k + .5) / n * 100}%"><div class="ic">${ic}</div>` +
      `<div class="nm">${nm}</div><div class="out" id="${p}o${k}"></div></div>`).join("") + `<div class="pkt" id="${p}pkt"></div>`;
}
function resetTrack(p, n) {
  const f = $(p + "fill"), k = $(p + "pkt");
  f.style.transition = k.style.transition = "none"; f.style.width = "0"; k.style.left = (50 / n) + "%"; k.style.opacity = 0;
  void f.offsetWidth; f.style.transition = k.style.transition = "";
  for (let j = 0; j < n; j++) { $(p + j).className = "node"; $(p + "o" + j).innerHTML = ""; }
}
async function hop(p, n, k, out, ms = 430) {
  const pkt = $(p + "pkt"); pkt.style.opacity = 1; pkt.style.left = ((k + .5) / n * 100) + "%";
  $(p + "fill").style.width = (k / (n - 1) * 100) + "%";
  await sleep(ms * .75);
  for (let j = 0; j < n; j++) $(p + j).className = "node" + (j < k ? " done" : j === k ? " on" : "");
  if (out != null) $(p + "o" + k).innerHTML = out;
  await sleep(ms * .25);
}

// ── ② DB 표 ──
function buildTables() {
  $("tgrid").innerHTML = Object.entries(T_COLS).map(([t, cols]) =>
    `<div class="tcard" id="tc_${t}"><h4>${t}<span id="tn_${t}">0행</span></h4><div class="tw"><table class="t"><colgroup>` +
    cols.map(c => `<col style="width:${T_W[c] || "auto"}">`).join("") + `</colgroup><thead><tr>` +
    cols.map(c => `<th>${c}</th>`).join("") + `</tr></thead><tbody id="tb_${t}"></tbody></table></div></div>`).join("");
  for (const k in count) delete count[k];
}
function insertRows(rows) {
  for (const {table, row} of rows) {
    if (!T_COLS[table]) continue;
    count[table] = (count[table] || 0) + 1;
    $("tn_" + table).textContent = count[table] + "행";
    const tr = `<tr class="new">${T_COLS[table].map(c => `<td title="${esc(row[c] ?? "")}">${esc(row[c] ?? "")}</td>`).join("")}</tr>`;
    $("tb_" + table).insertAdjacentHTML("afterbegin", tr);
    const card = $("tc_" + table); card.classList.add("bump"); setTimeout(() => card.classList.remove("bump"), 700);
  }
}

// ── 시작 카드 ──
function resetView() {
  running = false; playing = false; endEarly = false; i = -1; results = []; if (audio) audio.pause();
  for (const k in lv) delete lv[k];
  $("feed").innerHTML = ""; counts();
  $("play").textContent = "▶ 수신 시작"; $("live").classList.remove("on"); $("livet").textContent = "대기";
  $("ai").className = "ai"; $("ai").innerHTML = '<div class="ph" style="margin:0">🤖 AI 판정</div><div class="ex">무전 수신 대기 중입니다.</div>';
  document.querySelectorAll(".bubble").forEach(x => x.classList.remove("show")); setTalk(null);
  $("scn").textContent = cur.scene;
  const here = new Set(radiosOf(cur));
  document.querySelectorAll(".worker").forEach(p => p.style.display = here.has(p.id.slice(1)) ? "" : "none");
  board(cur.initial);
  $("rframe").classList.remove("vis"); $("rframe").removeAttribute("src"); $("rwait").style.display = "flex";
  toScreen(1, true);
}
function introHTML() {
  const who = radiosOf(cur).map(r => `<span>${r === "A" ? "🎧 운전실 (A)" : "📻 작업자 " + r}</span>`).join("");
  const ini = cur.initial.map(e => {
    const lock = e.why.length > 0;
    const st = lock ? e.why.map(w => (ICON[w] || "") + " " + w.replace(" 진행 중", "")).join(" ") : "✅ 가동 가능";
    return `<div class="${lock ? "lock" : ""}"><b>${esc(e.name)}</b>${st}</div>`;
  }).join("");
  const step = SCENES.length > 1 ? ` · ${si + 1} / ${SCENES.length}` : "";
  return `<div class="k">시나리오 ${esc(cur.scene)}${step}</div><div class="t">${esc(cur.title)}</div>
    <div class="s">${esc(cur.story)}</div>
    <div class="sec">등장인물</div><div class="who">${who}</div>
    <div class="sec">시작할 때 설비 상태</div><div class="ini">${ini}</div>
    <button id="go">▶ 무전 수신 시작</button><div class="h">🔊 녹음과 경보음이 나옵니다 · ① 현장 무전 → ② DB 실시간 적재 → ③ 보고서 초안</div>`;
}
async function showIntro(k, auto) {
  gen++; const g = gen;
  si = k; cur = await api(`/api/start?scene=${SCENES[k].scene}`);   // 서버: 설비 상태를 장면 시작 상태로
  if (g !== gen) return;
  resetView();
  $("cbox").innerHTML = introHTML(); $("cover").classList.remove("hide");
  $("go").onclick = () => { unlockAudio(); startScene(); };
  if (!auto) return;
  let left = 8;
  const tick = () => {
    if (g !== gen || $("cover").classList.contains("hide")) return;
    if (left <= 0) { startScene(); return; }
    $("go").textContent = `▶ ${left}초 후 시작`; left--; setTimeout(tick, 1000);
  };
  tick();
}
function unlockAudio() { if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)(); }
function startScene() {
  gen++; $("cover").classList.add("hide");
  playing = true; $("play").textContent = "⏸ 일시정지"; runScene(gen);
}

// ── ① 현장 무전: 무전 1건 = 녹음 재생 + 서버 처리 (교정 → CLS → 추출 → DB 저장 → 판정) ──
async function playMsg(f, idx, g) {
  const b = $("b" + f.radio) || document.createElement("div");
  document.querySelectorAll(".bubble").forEach(x => x.classList.remove("show"));
  setTalk(f.radio);
  $("ai").className = "ai";
  $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI 판정</div><div class="lv" style="font-size:18px">📡 ${NAME[f.radio]} 무전 수신 · 음성 인식 중<span class="dots"></span></div>`;
  b.textContent = "📡 …"; b.classList.add("show");
  if (audio) audio.pause();
  audio = new Audio(f.audio); audio.play().catch(() => {});
  const dur = Math.max(f.sec * 1000, 1200), t0 = performance.now();
  const res = await api(`/api/step?i=${idx}`);
  if (g !== gen) return;
  results.push(res);
  const s = res.msg, left = Math.max(dur - (performance.now() - t0), 500);
  await Promise.all([typeText(b, s.text, left * .85), sleep(left)]);
  if (g !== gen) return;
  setTalk(null);
  const eqs = eqText(s);
  $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI 판정</div><div class="lv" style="font-size:18px">분석 중<span class="dots"></span></div>
    <div class="meta">분류 ${s.cls} · 설비 ${eqs}${s.action !== "없음" ? " · 행위 " + s.action : ""} · 설비 상태 DB 조회</div>`;
  board(s.board, s.equip_ids);
  await sleep(650); if (g !== gen) return;
  const [ic, head, cls, color] = LV[s.level];
  lv[s.level] = (lv[s.level] || 0) + 1; counts();
  $("ai").className = "ai " + cls;
  $("ai").innerHTML = `<div class="ph" style="margin:0;color:#fff;opacity:.8">🤖 AI 판정 · #${s.msg_id}</div><div class="lv">${ic} ${head}</div>
    <div class="ex">${esc(s.explain)}</div><div class="meta">분류 ${s.cls} · 설비 ${eqs}${s.action !== "없음" ? " · 행위 " + s.action : ""}${s.llm ? " · 🧠 LLM 교차 확인: " + esc(s.llm) : ""}</div>`;
  alarm(s.level, s);
  const to = {"A": "운전실", "B": "B", "C": "C"}[s.to];
  feedLine(`<span class="t">${hhmmss()}</span><span class="id">#${s.msg_id}</span><span class="w">${NAME[s.radio]}${to ? " → " + to : ""}</span>
    <span class="tx">${esc(s.text)}</span><span class="lvb" style="background:${color}33;color:${color}">${ic} ${s.level}</span>`);
  await sleep(s.level === "충돌" ? 3000 : s.level === "불일치" ? 2300 : 1500);
  for (const ev of res.events) {
    if (g !== gen) return;
    board(ev.board, ev.equip_ids);
    feedLine(`<span class="t">${hhmmss()}</span><span>⏺ ${esc(ev.text)}</span>`, "ev");
    await sleep(1500);
  }
}
const eqText = s => [...s.equip, ...s.guess.map(x => x + "(추정)"), ...s.unknown.map(u => u + "(없는 설비)")].join(", ") || "-";

async function runScene(g) {
  running = true;
  $("live").classList.add("on"); $("livet").textContent = "LIVE";
  try {
    while (i < cur.files.length - 1 && !endEarly) {
      if (!playing) { $("livet").textContent = "일시정지"; await sleep(250); if (g !== gen) return; continue; }
      $("livet").textContent = "LIVE";
      i++; await playMsg(cur.files[i], i, g);
      if (g !== gen) return;
    }
  } catch (e) { $("ai").innerHTML = `<div class="ex">⚠ 서버 오류: ${esc(e.message)}</div>`; return; }
  setTalk(null); document.querySelectorAll(".bubble").forEach(x => x.classList.remove("show"));
  $("play").textContent = "✔ 교신 끝"; $("live").classList.remove("on");
  if (!results.length) return;
  // 교신 종료 판단: 마지막 무전 뒤 SILENCE초 동안 무전이 없으면 끝난 것으로 본다 (📝 교신 마감 버튼이면 바로)
  if (!endEarly) {
    $("livet").textContent = "무전 없음";
    for (let k = SILENCE; k > 0; k--) {
      $("ai").className = "ai";
      $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI 판정</div><div class="lv" style="font-size:18px">📡 무전 대기 중 · ${k}초</div>
        <div class="silence">${SILENCE}초 동안 무전이 없으면 교신이 끝난 것으로 보고 다음 단계로 넘어갑니다.</div>`;
      await sleep(1000); if (g !== gen) return;
    }
  }
  $("livet").textContent = "교신 종료";
  $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI</div><div class="lv" style="font-size:20px">✅ 교신 종료 감지</div>
    <div class="silence">무전 ${results.length}건 → ② DB 적재 과정으로 넘어갑니다</div>`;
  await sleep(1800); if (g !== gen) return;
  await toScreen(2); if (g !== gen) return;
  await playDB(g); if (g !== gen) return;
  await sleep(1200); if (g !== gen) return;
  await toScreen(3); if (g !== gen) return;
  await playReport(g);
}

// ── ② DB 실시간 적재: 무전마다 파이프라인 선을 따라 → 표에 INSERT ──
async function playDB(g) {
  const n = P_NODES.length;
  buildTrack("ptrack", P_NODES, "p"); buildTables(); $("jl").innerHTML = "";
  const jc = {};
  for (const [k, res] of results.entries()) {
    const s = res.msg, [ic, , , color] = LV[s.level];
    $("pmeta").textContent = `무전 ${k + 1} / ${results.length}`;
    resetTrack("p", n);
    await hop("p", n, 0, `#${s.msg_id} · ${NAME[s.radio]}`); if (g !== gen) return;
    await hop("p", n, 1, `“${esc(s.text)}”`); if (g !== gen) return;
    await hop("p", n, 2, esc(s.cls)); if (g !== gen) return;
    await hop("p", n, 3, esc(eqText(s)) + (s.action !== "없음" ? ` · ${esc(s.action)}` : "")); if (g !== gen) return;
    await hop("p", n, 4, `<b style="color:${color}">${ic} ${s.level}</b>`); if (g !== gen) return;
    if (s.level === "충돌" || s.level === "불일치") { alarm(s.level, s); await sleep(900); }
    await hop("p", n, 5, `INSERT ${s.db.length}행`); if (g !== gen) return;
    insertRows(s.db);
    jc[s.level] = (jc[s.level] || 0) + 1;
    $("jcnt").textContent = ["충돌", "불일치", "확인 필요"].map(x => `${LV[x][0]} ${jc[x] || 0}`).join("  ");
    $("jl").insertAdjacentHTML("afterbegin", `<div class="new"><span>#${s.msg_id}</span><span class="tx">${esc(s.text)}</span>
      <span class="lvb" style="background:${color}33;color:${color};font-weight:800;padding:1px 8px;border-radius:8px">${ic} ${s.level}</span></div>`);
    await sleep(450);
    for (const ev of res.events) {
      if (g !== gen) return;
      resetTrack("p", n);
      await hop("p", n, 0, `⏺ ${esc(ev.text.replace("담당자 버튼 · ", ""))}`, 300);
      $("p" + 0).className = "node on";
      await hop("p", n, 5, `INSERT state_change_log`, 700); if (g !== gen) return;
      insertRows(ev.db); await sleep(350);
    }
  }
  $("pmeta").textContent = `완료 · 무전 ${results.length}건 적재`;
}

// ── ③ 보고서 초안: live.db → 위험 상황 추리기 → 보고서 작성 → 보고서 ──
async function playReport(g) {
  const n = R_NODES.length;
  buildTrack("rtrack", R_NODES, "r"); resetTrack("r", n);
  $("rside").innerHTML = `<div class="hd">📝 보고서 초안 작성 중<span class="dots"></span></div>`;
  await hop("r", n, 0, `무전 ${results.length}건 · 판정 ${results.length}건 · 버튼 ${count.state_change_log || 0}건`, 700);
  let r;
  try { r = await api("/api/report"); } catch (e) { $("rside").innerHTML = `<div class="hd">⚠ ${esc(e.message)}</div>`; return; }
  if (g !== gen) return;
  const c = r.count, risky = (c["충돌"] || 0) + (c["불일치"] || 0) + (c["확인 필요"] || 0);
  await hop("r", n, 1, `위험 상황 ${risky}건<br>🔴 ${c["충돌"] || 0} · 🟠 ${c["불일치"] || 0} · 🟡 ${c["확인 필요"] || 0}`, 900); if (g !== gen) return;
  await hop("r", n, 2, `개요 · 근거 · 설비 상태 변경 · 권고 조치`, 900); if (g !== gen) return;
  await hop("r", n, 3, `📄 보고서 (웹 · 엑셀)${r.excels.length ? "<br>📥 위험성평가 엑셀" : ""}`, 900); if (g !== gen) return;
  $("rmeta").textContent = r.name;
  $("rframe").onload = () => { $("rwait").style.display = "none"; $("rframe").classList.add("vis"); };
  $("rframe").src = r.html;
  const keys = ["충돌", "불일치", "확인 필요", "정상", "기록"];
  const rk = r.risky.map(x => `<div>${LV[x.level][0]} “${esc(x.text)}”<br><span style="opacity:.7">${esc(x.reason)}</span></div>`).join("");
  const next = si < SCENES.length - 1;
  $("rside").innerHTML = `<div class="hd">${esc(r.headline)}</div>
    <div class="cn">${keys.map(k => `<span>${LV[k][0]} ${k} ${c[k] || 0}</span>`).join("")}</div>
    ${rk ? `<div class="rk">${rk}</div>` : ""}
    <a href="${r.html}" target="_blank">📄 보고서 새 창으로 열기</a>
    <a class="x" style="background:#15803d" href="${r.xlsx}">📊 보고서 엑셀 받기</a>
    ${r.excels.map(u => `<a class="x" href="${u}">📥 위험성평가 엑셀 받기</a>`).join("")}
    <div class="sp"></div>
    ${next ? `<button id="nx">▶ 다음 장면 (${NEXT_SEC}초)</button>` : `<button id="again">↺ 처음부터</button>`}
    <div class="h">보고서·엑셀: reports/ 폴더 · 위험성평가 초안: live.db의 risk_draft 표</div>`;
  if ($("again")) $("again").onclick = () => showIntro(0, false);
  if ($("nx")) {
    $("nx").onclick = () => showIntro(si + 1, true);
    let left = NEXT_SEC;
    const tick = () => {
      if (g !== gen || !$("nx")) return;
      if (left <= 0) { showIntro(si + 1, true); return; }
      $("nx").textContent = `▶ 다음 장면 (${left}초)`; left--; setTimeout(tick, 1000);
    };
    tick();
  }
}

$("play").onclick = () => {
  unlockAudio();
  if (!$("cover").classList.contains("hide")) { if ($("go")) startScene(); return; }
  if (!running) return;
  playing = !playing; $("play").textContent = playing ? "⏸ 일시정지" : "▶ 계속";
};
$("rep").onclick = () => { if (running && results.length) { endEarly = true; playing = true; } };
$("restart").onclick = () => showIntro(0, false);
window.addEventListener("resize", placeScreens);
counts();
showIntro(0, false).then(() => {   // 주소 뒤에 ?auto 를 붙이면 1초 뒤 자동 시작 (소리는 브라우저 설정에 따라 막힐 수 있음)
  if (new URLSearchParams(location.search).has("auto")) setTimeout(() => { if ($("go")) startScene(); }, 1000);
});
"""


# ── 서버 ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    live = None
    scenes = DEFAULT_SCENES

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self._send(200, page(self.scenes), "text/html; charset=utf-8")
            elif u.path == "/api/start":
                self._json(self.live.start(q["scene"]))
            elif u.path == "/api/step":
                self._json(self.live.step(int(q["i"])))
            elif u.path == "/api/report":
                self._json(self.live.report())
            elif u.path.startswith("/audio/"):
                f = REC_DIR / Path(unquote(u.path[7:])).name
                self._send(200, f.read_bytes(), "audio/wav")
            elif u.path == "/char/bear":
                self._send(200, anim.CHAR_PNG.read_bytes(), "image/png")
            elif u.path == "/char/worker":
                f, mime = next(((f, m) for f, m in anim.WORKER_IMGS if f.exists()), (anim.CHAR_PNG, "image/png"))
                self._send(200, f.read_bytes(), mime)
            elif u.path.startswith("/reports/"):
                f = REPORT_DIR / Path(unquote(u.path[9:])).name
                if f.suffix == ".xlsx":
                    self._send(200, f.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}"})
                else:
                    self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except FileNotFoundError:
            self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(500, str(e), "text/plain; charset=utf-8")


def lan_ip():
    """이 Mac의 와이파이 IP (다른 기기에서 접속할 주소)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 실제로 보내지 않음, 나가는 인터페이스만 고른다
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description="실시간 무전 관제 시연 (DB 실시간 적재 + 교신 종료 시 보고서 초안)")
    p.add_argument("--scenes", default=",".join(DEFAULT_SCENES), help="재생할 장면 (예: S01,S06,S12)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--stt", action="store_true", help="녹음을 그 자리에서 Whisper로 변환 (기본: llm_results.csv 결과 사용)")
    p.add_argument("--fresh", action="store_true", help="live.db 를 새로 만든다 (기존 기록 삭제)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--share", action="store_true",
                   help="같은 와이파이의 다른 기기(노트북·휴대폰)에서도 열 수 있게 (0.0.0.0 으로 열기)")
    a = p.parse_args()
    scenes = [s.strip().upper() for s in a.scenes.split(",") if s.strip()]
    bad = [s for s in scenes if s not in SCENES]
    if bad:
        sys.exit(f"없는 장면: {bad} (가능: {', '.join(SCENES)})")
    Handler.live = Live(use_stt=a.stt, fresh=a.fresh)
    Handler.scenes = scenes
    url = f"http://localhost:{a.port}"
    print(f"실시간 관제 시연: {url}   장면 {', '.join(scenes)}   DB {LIVE_DB}   보고서 {REPORT_DIR}/")
    if a.share:
        print(f"다른 기기에서 열기 (같은 와이파이): http://{lan_ip()}:{a.port}")
        print("  ※ 화면은 여러 대가 봐도 되지만, 교신 진행은 서버 하나라 한 사람만 [▶ 시작]을 누르세요.")
    print("입력: " + ("녹음 → Whisper 실시간 변환" if a.stt else "llm_results.csv (미리 돌린 STT·LLM 결과)") + "   종료: Ctrl+C")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("0.0.0.0" if a.share else "127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
