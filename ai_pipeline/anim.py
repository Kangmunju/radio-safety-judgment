"""관제 화면 시뮬레이션: 운전실과 현장 작업자의 무전을 AI가 듣고 판정·경보·DB 적재까지 실시간으로 보여 준다.

분석은 진짜 파이프라인(팀 CLS → 설비·행위 추출 → 팀 DB → 판정)이 장면 시작 때 한 번에 돌린 결과이고,
화면(HTML/JS)은 그 결과를 시간 순서대로 재생한다 — 녹음 재생, 말풍선, AI 판정, 경보, DB INSERT.
DB 패널에 보이는 값은 실제로 DB에 들어간 행을 그대로 읽어 온 것이다.
"""
import base64
import html
import json
import re
from pathlib import Path

import db
from judge import Judge
from llm_post import EQUIPMENT
from pipeline import SCENARIO_BUTTONS, new_state, process_message, process_precomputed

BASE = Path(__file__).resolve().parent
from team_cls import REC_DIR  # noqa: E402  녹음 폴더 (없으면 팀 저장소 recordings/)
CHAR_PNG = BASE / "assets" / "bear.png"  # 운전실 캐릭터 (배경 투명)
# 현장 작업자 캐릭터: worker.png가 있으면 그것을, 없으면 worker.svg를 쓴다 (둘 다 없으면 운전실 캐릭터)
WORKER_IMGS = [(BASE / "assets" / "worker.png", "image/png"), (BASE / "assets" / "worker.svg", "image/svg+xml")]


# 장면별 상황 소개 (시작 카드에 표시). 무슨 위험이 생기는지는 말하지 않는다 — AI가 잡아내는 걸 보여 주기 위해.
STORY = {
    "S01": "운전실(A)이 작업자 B에게 3번 컨베이어 롤러 점검과 낙광 청소를 맡깁니다. "
           "B는 정비를 위해 컨베이어를 세우고 LOTO를 겁니다.",
    "S02": "운전실이 작업자 B와 C에게 여러 설비의 점검과 정비를 요청합니다.",
    "S03": "운전실이 몇몇 설비를 세우라고 지시합니다. 4번 컨베이어는 정비 중이고 LOTO가 걸려 있습니다.",
    "S04": "4번 컨베이어는 정비 중이고 LOTO가 걸려 있습니다. 운전실과 작업자들이 4번 컨베이어에 대해 교신합니다.",
    "S05": "2번 송풍기는 베어링 교체를 위한 작업허가가 진행 중입니다. 운전실과 작업자가 송풍기 기동에 대해 교신합니다.",
    "S06": "운전실이 여러 설비의 가동과 정지를 지시하고, 작업자가 지시를 복창합니다.",
    "S07": "운전실이 설비 가동과 정지를 지시하고, 작업자가 지시를 복창합니다.",
    "S08": "운전실이 바쁘게 여러 지시를 내립니다. 4번 컨베이어는 정비 중이고 LOTO가 걸려 있습니다.",
    "S09": "운전실이 설비 가동을 지시하고 작업자의 복창을 기다립니다.",
    "S10": "운전실이 현장에서 실제로 쓰는 줄임말과 다양한 표현('1컨', '세워', '살려', '런' 등)으로 지시합니다.",
    "S11": "교신 확인, 순찰 보고, 교대 같은 일상 무전이 오갑니다.",
    "S12": "작업자 B가 LOTO를 건 3번 컨베이어 안에서 정비 작업을 하고 있습니다. "
           "다른 작업자는 3번 컨베이어 주변에 쌓인 낙광을 치우고 있습니다.",
    "S13": "운전실이 설비 가동을 지시합니다.",
}


def pretty(text):
    return re.sub(r"[A-Z]{2}-\d{2}", lambda m: EQUIPMENT.get(m.group(0), m.group(0)), text)


def explain(m, conn):
    """판정을 현장 사람이 읽기 쉬운 말로."""
    eq = ", ".join(EQUIPMENT[e] for e in m["equipment_ids"]) or "설비"
    lv = m["level"]
    if lv == "충돌":
        guessed = not m["equipment_ids"] and m.get("equipment_guess")
        ids = m["equipment_ids"] or m.get("equipment_guess", [])
        target = ", ".join(EQUIPMENT[e] for e in ids if db.blocked_reasons(conn, e)) or eq
        if guessed:
            return (f"설비 번호가 잘 들리지 않았지만, 직전 교신으로 보면 {target}일 가능성이 큽니다. "
                    f"{target}은(는) 잠겨 있으니 가동하기 전에 바로 확인하세요.")
        if m["cls"] == "복창":
            return f"작업자가 {target} 가동을 그대로 받아들였습니다. 이대로 돌리면 정비 중인 작업자가 다칠 수 있습니다."
        who = "요청" if m["radio"] != "A" else "지시"
        return f"{target}은(는) LOTO·작업허가로 잠겨 있는데 가동 {who}가 나왔습니다. 안에 작업자가 있으면 끼임 사고로 이어집니다."
    if lv == "불일치":
        return f"지시와 다르게 알아들었습니다. 이대로 움직이면 엉뚱한 설비가 돌거나 멈춥니다. ({pretty(m['reason'])})"
    if lv == "확인 필요":
        if m.get("equipment_guess") and not m["equipment_ids"]:
            g = ", ".join(EQUIPMENT[e] for e in m["equipment_guess"])
            return f"설비 번호가 확실하지 않습니다. 직전 교신으로 보면 {g}일 것 같지만, 지시자에게 다시 확인하세요."
        return f"어떤 설비인지 확실하지 않습니다. 지시자에게 다시 확인하세요. ({pretty(m['reason'])})"
    if lv == "정상":
        if m["cls"] == "복창":
            return f"지시를 정확히 복창했습니다. ({eq})"
        return f"{eq}에 위험한 상태가 없습니다. 복창을 기다립니다." if m["cls"] == "가동지시" else pretty(m["reason"])
    return "판정 대상이 아닌 무전입니다. 기록으로만 남깁니다." if m["cls"] != "복창" else pretty(m["reason"])


def board_state(conn):
    return [{"id": b["equipment_id"], "name": b["name"], "why": db.blocked_reasons(conn, b["equipment_id"])}
            for b in db.board(conn)]


def audio_uri(file):
    path = REC_DIR / file
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode() if path.exists() else ""


def _rows(conn, sql, args=()):
    return [{k: ("" if v is None else v) for k, v in dict(r).items()} for r in conn.execute(sql, args)]


def inserted_rows(conn, msg_id):
    """무전 한 건으로 DB에 실제로 들어간 행."""
    out = [{"table": "radio_message", "row": r} for r in _rows(conn,
           "SELECT msg_id, radio_id, addressee_id, cls, action, stt_text FROM radio_message WHERE msg_id = ?", (msg_id,))]
    out += [{"table": "message_equipment", "row": r} for r in _rows(conn,
            "SELECT link_id, msg_id, equipment_id, mention_text FROM message_equipment WHERE msg_id = ?", (msg_id,))]
    out += [{"table": "judgment", "row": r} for r in _rows(conn,
            "SELECT judgment_id, msg_id, ref_msg_id, level, reason FROM judgment WHERE msg_id = ?", (msg_id,))]
    return out


def build_timeline(clf, ext, scene, rows, source, explain, pre=None):
    """장면 전체를 분석해 재생용 단계 목록을 만든다. explain(m, conn) → 쉬운 설명.
    pre: {파일: llm_results.csv 한 줄} — 주면 모델 없이 그 결과(교정·CLS·설비·행위)로 DB 저장·판정만 한다."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.reset_state(conn, scene)
    judge, state = Judge(conn), new_state()
    initial = board_state(conn)
    steps = []
    for row in rows:
        radio = re.search(r"_([A-Z])\.wav$", row["file"]).group(1)
        if pre is not None:
            if row["file"] not in pre:
                continue  # LLM 결과 파일에 없는 무전은 건너뜀
            m = process_precomputed(conn, judge, state, scene, row["file"], radio, pre[row["file"]])
        else:
            raw = row["script"] if source == "대본 문장" else row["stt"]
            m = process_message(conn, clf, ext, judge, state, scene, row["file"], radio, raw)
        guess = [EQUIPMENT[e] for e in m["equipment_guess"]] if not m["equipment_ids"] else []
        steps.append({
            "type": "msg", "msg_id": m["msg_id"], "radio": radio, "to": m["addressee"], "text": m["corrected_text"],
            "cls": m["cls"], "equip": [EQUIPMENT[e] for e in m["equipment_ids"]], "guess": guess,
            "equip_ids": m["equipment_ids"] or m["equipment_guess"], "unknown": m["equipment_mentions"],
            "action": m["action"], "level": m["level"], "reason": pretty(m["reason"]), "explain": explain(m, conn),
            "audio": audio_uri(row["file"]), "sec": row["sec"], "board": board_state(conn),
            "db": inserted_rows(conn, m["msg_id"]), "llm": m.get("llm_check", ""),
        })
        for eid, act in SCENARIO_BUTTONS.get(row["file"], []):
            db.press_button(conn, eid, act, by="담당자", msg_id=m["msg_id"])
            log = _rows(conn, "SELECT log_id, equipment_id, action, msg_id, changed_by FROM state_change_log "
                              "ORDER BY log_id DESC LIMIT 1")
            steps.append({"type": "event", "text": f"담당자 버튼 · {EQUIPMENT[eid]} {db.BUTTON_LABEL[act]}",
                          "equip_ids": [eid], "board": board_state(conn),
                          "db": [{"table": "state_change_log", "row": r} for r in log]})
    conn.close()
    return {"initial": initial, "steps": steps}


def render(scenes):
    """scenes: [{"scene", "title", "story", "timeline"}, ...] — 여러 개면 순서대로 이어서 재생한다 (시연 영상용).
    장면마다: 상황 소개 카드(등장인물·시작 설비 상태) → 무전 재생 → 다음 장면 소개 … → 마지막에 결과 요약."""
    sc = [{"scene": s["scene"], "title": s["title"], "story": s["story"],
           "initial": s["timeline"]["initial"], "steps": s["timeline"]["steps"]} for s in scenes]
    data = json.dumps(sc, ensure_ascii=False).replace("</", "<\\/")
    radios = sorted({st["radio"] for s in sc for st in s["steps"] if st["type"] == "msg"} - {"A"}) or ["B"]
    workers = "".join(
        f"<div class='person worker' id='p{r}'><div class='bubble' id='b{r}'></div>"
        f"<div class='face flip'><img src='__WORKER__' alt='작업자 {r}'></div>"
        f"<div class='label'>📻 작업자 {r}</div></div>" for r in radios)
    char = ("data:image/png;base64," + base64.b64encode(CHAR_PNG.read_bytes()).decode()) if CHAR_PNG.exists() else ""
    worker = next(("data:%s;base64,%s" % (mime, base64.b64encode(f.read_bytes()).decode())
                   for f, mime in WORKER_IMGS if f.exists()), char)
    return (TEMPLATE.replace("__WORKERS__", workers).replace("__WORKER__", worker)
            .replace("__CHAR__", char).replace("__DATA__", data))


TEMPLATE = r"""
<!doctype html><html><head><meta charset="utf-8">
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;background:#070d18;color:#e5e7eb}
#app{position:relative;height:100vh;padding:10px 12px;overflow:hidden;display:flex;flex-direction:column;gap:10px}
/* 상단 바 */
.bar{display:flex;align-items:center;gap:14px;padding:8px 14px;border-radius:12px;background:#0f172a;border:1px solid #1e293b;font-size:13.5px}
.bar .sys{font-weight:900;font-size:16px}
.bar .sep{opacity:.25}
.live{display:inline-flex;align-items:center;gap:6px;font-weight:800;color:#94a3b8}
.live i{width:9px;height:9px;border-radius:50%;background:#475569;display:inline-block}
.live.on{color:#ef4444}.live.on i{background:#ef4444;animation:blink 1s infinite}
@keyframes blink{50%{opacity:.25}}
.bar .clock{font-variant-numeric:tabular-nums;font-weight:700}
.bar .sp{flex:1}
.bar .cnt span{margin-left:8px}
.bar button{background:#2563eb;color:#fff;border:0;border-radius:9px;padding:7px 14px;font-weight:800;cursor:pointer}
.bar button.sub{background:#334155}
/* 본문 격자 */
.grid{flex:1;display:grid;grid-template-columns:.95fr 1.05fr;grid-template-rows:1.15fr 1fr;gap:10px;min-height:0}
.panel{position:relative;border-radius:12px;background:#0f172a;border:1px solid #1e293b;padding:10px 12px;min-height:0;overflow:hidden}
.ph{font-size:12px;font-weight:800;letter-spacing:.3px;color:#94a3b8;margin-bottom:6px;display:flex;justify-content:space-between}
/* 현장 */
.stage{display:grid;grid-template-columns:1fr 1.4fr;gap:10px;height:calc(100% - 22px)}
.room,.field{position:relative;border-radius:10px;display:flex;align-items:flex-end;justify-content:center;gap:16px;padding-bottom:8px}
.room{background:linear-gradient(#1e293b,#0b1324)}
.field{background:linear-gradient(#1c2a1c,#0c160e)}
.room .tag,.field .tag{position:absolute;top:6px;left:9px;font-size:11px;opacity:.55}
.person{position:relative;display:flex;flex-direction:column;align-items:center}
.face{line-height:0}
.face img{height:118px;display:block;filter:drop-shadow(0 4px 6px rgba(0,0,0,.5))}
.face.flip img{transform:scaleX(-1)}
.label{font-size:12px;font-weight:800;margin-top:3px}
.person.talk .face{animation:bob .32s infinite alternate}
.person.talk .label{color:#60a5fa}
@keyframes bob{to{transform:translateY(-4px)}}
.bubble{position:absolute;bottom:150px;min-width:140px;max-width:250px;background:#f8fafc;color:#0f172a;border-radius:12px;
        padding:7px 10px;font-size:13.5px;font-weight:700;line-height:1.35;opacity:0;transition:opacity .2s;z-index:3}
.bubble.show{opacity:1}
.bubble::after{content:"";position:absolute;bottom:-7px;left:50%;margin-left:-7px;border:7px solid transparent;border-bottom:0;border-top-color:#f8fafc}
.waves{position:absolute;left:calc(41% - 20px);top:45%;width:40px;height:40px;opacity:0}
.waves.on{opacity:1}
.waves span{position:absolute;inset:0;border:3px solid #60a5fa;border-radius:50%;animation:wave 1s infinite;opacity:0}
.waves span:nth-child(2){animation-delay:.33s}.waves span:nth-child(3){animation-delay:.66s}
@keyframes wave{0%{transform:scale(.3);opacity:1}100%{transform:scale(1.7);opacity:0}}
/* AI 판정 + 무전 기록 */
.right1{display:flex;flex-direction:column;gap:8px}
.ai{border-radius:10px;padding:10px 14px;background:#111827;border:2px solid #334155;transition:all .3s;min-height:112px}
.ai .lv{font-size:23px;font-weight:900}
.ai .ex{font-size:14px;margin-top:5px;line-height:1.45}
.ai .meta{font-size:11.5px;opacity:.75;margin-top:5px}
.ai.충돌{background:#7f1d1d;border-color:#ef4444}.ai.불일치{background:#7c2d12;border-color:#f97316}
.ai.확인{background:#713f12;border-color:#eab308}.ai.정상{background:#14532d;border-color:#22c55e}.ai.기록{background:#1f2937;border-color:#6b7280}
.feed{flex:1;overflow:auto;font-size:13px;border-top:1px solid #1e293b;padding-top:4px}
.fl{display:grid;grid-template-columns:62px 34px 108px 1fr auto;gap:8px;align-items:center;padding:5px 2px;border-bottom:1px solid #162033}
.fl .t{font-variant-numeric:tabular-nums;opacity:.6}.fl .id{opacity:.5}.fl .w{font-weight:800}
.fl .tx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fl .lvb{font-size:12px;font-weight:800;padding:1px 8px;border-radius:8px;white-space:nowrap}
.fl.ev{grid-template-columns:62px 1fr;color:#93c5fd}
.fl.rx .tx{color:#93c5fd}
.fl.new{animation:newrow 1.2s}
@keyframes newrow{from{background:rgba(96,165,250,.25)}to{background:transparent}}
/* 설비 */
.equip{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;height:calc(100% - 22px)}
.eq{position:relative;border-radius:10px;padding:8px 9px;background:#0b1a12;border:2px solid #22c55e;transition:all .3s}
.eq .nm{font-size:13px;font-weight:800}
.eq .st{font-size:11.5px;margin-top:4px;line-height:1.35}
.eq .belt{height:6px;border-radius:3px;margin-top:6px;background:repeating-linear-gradient(90deg,#64748b 0 9px,transparent 9px 15px)}
.eq.lock{border-color:#ef4444;background:#2a1215}
.eq.lock .belt{background:repeating-linear-gradient(90deg,#7f1d1d 0 9px,transparent 9px 15px)}
.eq.hot{box-shadow:0 0 0 3px #60a5fa;animation:hot 1s infinite alternate}
@keyframes hot{to{box-shadow:0 0 0 6px rgba(96,165,250,.35)}}
.eq .pin{position:absolute;top:-11px;right:-4px;font-size:16px}
/* DB */
.dbcnt{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.dbcnt span{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;padding:2px 8px;border-radius:8px;background:#111827;border:1px solid #1e293b}
.dbcnt span.bump{animation:bump .9s}
@keyframes bump{from{background:#166534;border-color:#22c55e}to{background:#111827}}
.dblog{height:calc(100% - 52px);overflow:auto;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;line-height:1.5}
.dblog div{padding:3px 4px;border-bottom:1px solid #162033;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dblog .k{color:#22c55e;font-weight:800}.dblog .tb{color:#60a5fa;font-weight:800}
.dblog div.new{animation:newdb 1.4s}
@keyframes newdb{from{background:rgba(34,197,94,.3)}to{background:transparent}}
/* 경보 */
#flash{position:absolute;inset:0;pointer-events:none;opacity:0;z-index:10}
#flash.red{animation:flashR .45s 6 alternate}#flash.orange{animation:flashO .45s 3 alternate}#flash.yellow{animation:flashY .45s 2 alternate}
@keyframes flashR{to{opacity:1;box-shadow:inset 0 0 0 10px #ef4444,inset 0 0 160px rgba(239,68,68,.6)}}
@keyframes flashO{to{opacity:1;box-shadow:inset 0 0 0 8px #f97316,inset 0 0 120px rgba(249,115,22,.45)}}
@keyframes flashY{to{opacity:1;box-shadow:inset 0 0 0 6px #eab308}}
#alert{position:absolute;left:50%;top:44%;transform:translate(-50%,-50%) scale(.6);opacity:0;z-index:11;pointer-events:none;
       background:#dc2626;color:#fff;font-size:32px;font-weight:900;padding:16px 32px;border-radius:16px;transition:all .25s;text-align:center}
#alert.show{opacity:1;transform:translate(-50%,-50%) scale(1)}
#alert small{display:block;font-size:15px;font-weight:600;margin-top:6px}
.dots::after{content:'';animation:dots 1.2s steps(4,end) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
/* 시작 화면 */
#cover{position:absolute;inset:0;z-index:20;display:flex;align-items:center;justify-content:center;
       background:rgba(7,13,24,.9);backdrop-filter:blur(3px);transition:opacity .4s}
#cover.hide{opacity:0;pointer-events:none}
#cover .box{text-align:center;max-width:900px;padding:0 20px}
#cover .k{font-size:14px;opacity:.6;letter-spacing:1px}
#cover .t{font-size:30px;font-weight:900;margin-top:6px}
#cover .s{font-size:17px;opacity:.9;margin:14px 0 18px;line-height:1.6}
#cover .sec{font-size:12px;font-weight:800;color:#94a3b8;margin:14px 0 7px;letter-spacing:.3px}
#cover .who{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
#cover .who span{padding:6px 14px;border-radius:20px;background:#111827;border:1px solid #334155;font-weight:800;font-size:14px}
#cover .ini{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:24px}
#cover .ini div{padding:6px 8px;border-radius:9px;background:#0b1a12;border:1px solid #166534;font-size:12.5px;text-align:left}
#cover .ini div b{display:block;font-size:13.5px}
#cover .ini div.lock{background:#2a1215;border:2px solid #ef4444}
#cover table{margin:4px auto 20px;border-collapse:collapse;font-size:15px}
#cover td{padding:7px 14px;border-bottom:1px solid #1e293b;text-align:left}
#cover td.n{text-align:center;font-variant-numeric:tabular-nums;font-weight:800}
#cover button{font-size:24px;font-weight:900;padding:16px 40px;border:0;border-radius:16px;background:#2563eb;color:#fff;
              cursor:pointer;animation:go 1.6s infinite}
@keyframes go{0%{box-shadow:0 0 0 0 rgba(37,99,235,.7)}70%{box-shadow:0 0 0 22px rgba(37,99,235,0)}}
#cover .h{font-size:12.5px;opacity:.55;margin-top:12px}
</style></head><body><div id="app">

<div class="bar">
  <span class="sys">📻 무전 안전 모니터링 시스템</span><span class="sep">|</span>
  <span>원료공장 · 무전 CH-1</span><span class="sep">|</span>
  <span class="live" id="live"><i></i><span id="livet">대기</span></span><span class="sep">|</span>
  <span class="clock" id="clock"></span><span class="sep">|</span>
  <span>시나리오 <span id="scn"></span></span>
  <span class="sp"></span>
  <span class="cnt" id="cnt"></span>
  <button id="play">▶ 수신 시작</button><button class="sub" id="restart">↺</button>
</div>

<div class="grid">
  <div class="panel"><div class="ph"><span>🎥 현장 상황</span><span id="who"></span></div>
    <div class="stage">
      <div class="room"><div class="tag">🏢 운전실</div>
        <div class="person" id="pA"><div class="bubble" id="bA"></div><div class="face"><img src="__CHAR__" alt="운전실"></div><div class="label">🎧 운전실 (A)</div></div></div>
      <div class="field"><div class="tag">🏭 원료 설비 현장</div>__WORKERS__</div>
    </div>
    <div class="waves" id="waves"><span></span><span></span><span></span></div>
  </div>

  <div class="panel right1">
    <div class="ai" id="ai"><div class="ph" style="margin:0">🤖 AI 판정</div><div class="ex">무전 수신 대기 중입니다.</div></div>
    <div class="ph" style="margin:0"><span>📻 실시간 무전 기록</span><span>최신이 위</span></div>
    <div class="feed" id="feed"></div>
  </div>

  <div class="panel"><div class="ph"><span>🏭 설비 상태</span><span>🔒 LOTO · 🔧 정비 중 · 📝 작업허가 · 📍 지금 무전 대상</span></div>
    <div class="equip" id="equip"></div></div>

  <div class="panel"><div class="ph"><span>📦 DB 적재 (팀 DB 구조 · SQLite)</span><span>실제로 들어간 행</span></div>
    <div class="dbcnt" id="dbcnt"></div>
    <div class="dblog" id="dblog"></div></div>
</div>

<div id="flash"></div><div id="alert"></div>
<div id="cover"><div class="box" id="cbox"></div></div>
</div>
<script>
const SC = __DATA__;   // 장면 목록 (하나 또는 여러 개)
let si = 0, T = SC[0], gen = 0;   // gen: 처음으로 돌리면 진행 중이던 재생·카운트다운을 멈추는 번호
const INTRO_SEC = 8;   // 두 번째 장면부터 소개 카드를 보여 주는 시간
const LV = {"충돌":["🔴","위험! 가동 금지","충돌","#ef4444"],"불일치":["🟠","복창이 지시와 다릅니다","불일치","#f97316"],
            "확인 필요":["🟡","다시 확인하세요","확인","#eab308"],"정상":["🟢","안전합니다","정상","#22c55e"],"기록":["⚪","기록만 합니다","기록","#6b7280"]};
const ICON = {"정비 중":"🔧","LOTO 적용":"🔒","작업허가 진행 중":"📝"};
const NAME = {"A":"운전실","B":"작업자 B","C":"작업자 C"};
const TABLES = ["radio_message","message_equipment","judgment","state_change_log"];
let i = -1, playing = false, running = false, ctx = null, audio = null;
const count = {}, lv = {};
const $ = id => document.getElementById(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const hhmmss = () => new Date().toTimeString().slice(0, 8);

setInterval(() => { const d = new Date(); $("clock").textContent = d.toLocaleDateString("ko-KR") + " " + d.toTimeString().slice(0, 8); }, 500);

function board(b, hot=[]) {
  $("equip").innerHTML = b.map(e => {
    const lock = e.why.length > 0, h = hot.includes(e.id);
    const st = lock ? e.why.map(w => (ICON[w]||"") + " " + w.replace(" 진행 중","")).join("<br>") : "✅ 가동 가능";
    const ic = e.id.startsWith("CR") ? "⚙️ " : e.id.startsWith("FN") ? "🌀 " : "";
    return `<div class="eq ${lock?"lock":""} ${h?"hot":""}">${h?'<div class="pin">📍</div>':""}
      <div class="nm">${ic}${e.name}</div><div class="st">${st}</div>${e.id.startsWith("CV")?'<div class="belt"></div>':""}</div>`;
  }).join("");
}
function dbCounts(bump=[]) {
  $("dbcnt").innerHTML = TABLES.map(t => `<span class="${bump.includes(t)?"bump":""}">${t} ${count[t]||0}</span>`).join("");
}
function dbInsert(rows) {
  const touched = [];
  for (const {table, row} of rows) {
    count[table] = (count[table] || 0) + 1; touched.push(table);
    const vals = Object.entries(row).map(([k, v]) => `${k}=${typeof v === "number" ? v : '"' + esc(String(v).slice(0, 40)) + '"'}`).join(", ");
    $("dblog").insertAdjacentHTML("afterbegin", `<div class="new"><span class="k">INSERT</span> <span class="tb">${table}</span> (${vals})</div>`);
  }
  dbCounts(touched);
}
function counts() {
  $("cnt").innerHTML = ["충돌","불일치","확인 필요"].map(k => `<span>${LV[k][0]} ${lv[k]||0}</span>`).join("");
}
function beep(f1, f2, dur, type="sawtooth", vol=.15) {
  if (!ctx) return;
  const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime;
  o.type = type; o.frequency.setValueAtTime(f1, t); o.frequency.linearRampToValueAtTime(f2, t + dur);
  g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(.001, t + dur);
  o.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + dur);
}
function alarm(level, s) {
  const f = $("flash"), a = $("alert");
  f.className = ""; void f.offsetWidth;
  if (level === "충돌") {
    f.className = "red";
    for (let k = 0; k < 3; k++) { setTimeout(() => beep(650, 1250, .5), k*560); setTimeout(() => beep(1250, 650, .5), k*560+280); }
    const who = s.equip.length ? s.equip.join(", ") : s.guess.length ? s.guess.join(", ") + " (추정)" : "설비";
    a.style.background = "#dc2626"; a.innerHTML = `⚠ 위험! 가동 금지<small>${who} — LOTO·작업허가 중</small>`;
    a.classList.add("show"); setTimeout(() => a.classList.remove("show"), 2600);
  } else if (level === "불일치") {
    f.className = "orange"; beep(880, 880, .22, "square", .12); setTimeout(() => beep(660, 660, .3, "square", .12), 260);
    a.style.background = "#ea580c"; a.innerHTML = "⚠ 복창 불일치<small>지시와 다르게 알아들었습니다</small>";
    a.classList.add("show"); setTimeout(() => a.classList.remove("show"), 1900);
  } else if (level === "확인 필요") { f.className = "yellow"; beep(740, 740, .25, "triangle", .14); }
  else if (level === "정상") { beep(880, 1320, .12, "sine", .08); }
}
function feedLine(html, cls="") { $("feed").insertAdjacentHTML("afterbegin", `<div class="fl new ${cls}">${html}</div>`); }
function setTalk(r, to) {
  document.querySelectorAll(".person").forEach(p => p.classList.remove("talk"));
  if (!r) { $("waves").classList.remove("on"); $("who").textContent = ""; return; }
  $("p" + r)?.classList.add("talk"); $("waves").classList.add("on");
  $("who").textContent = `🔴 ${NAME[r]} 송신 중`;
}
async function typeText(el, text, ms) {
  el.textContent = ""; const per = Math.max(18, ms / Math.max(text.length, 1));
  for (const ch of text) { if (!running) return; el.textContent += ch; await sleep(per); }
}
async function play(s) {
  if (s.type === "event") {
    board(s.board, s.equip_ids); dbInsert(s.db);
    feedLine(`<span class="t">${hhmmss()}</span><span>⏺ ${esc(s.text)}</span>`, "ev");
    await sleep(1500); return;
  }
  // 1) 수신: 말하는 사람 + 녹음 + 말풍선
  const b = $("b" + s.radio);
  document.querySelectorAll(".bubble").forEach(x => x.classList.remove("show"));
  setTalk(s.radio, s.to);
  $("ai").className = "ai";
  $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI 판정</div><div class="lv" style="font-size:18px">📡 ${NAME[s.radio]} 무전 수신 · 음성 인식 중<span class="dots"></span></div>`;
  if (b) b.classList.add("show");
  if (audio) audio.pause();
  if (s.audio) { audio = new Audio(s.audio); audio.play().catch(() => {}); }
  const dur = Math.max(s.sec * 1000, 1200);
  await Promise.all([typeText(b || document.createElement("div"), s.text, dur * .85), sleep(dur)]);
  setTalk(null);
  // 2) 분석
  const eqs = [...s.equip, ...s.guess.map(g => g + "(추정)"), ...s.unknown.map(u => u + "(없는 설비)")].join(", ") || "-";
  $("ai").innerHTML = `<div class="ph" style="margin:0">🤖 AI 판정</div><div class="lv" style="font-size:18px">분석 중<span class="dots"></span></div>
    <div class="meta">분류 ${s.cls} · 설비 ${eqs}${s.action !== "없음" ? " · 행위 " + s.action : ""} · 설비 상태 DB 조회</div>`;
  board(s.board, s.equip_ids);
  await sleep(650);
  // 3) 판정 + 경보 + 기록 + DB 적재
  const [ic, head, cls, color] = LV[s.level];
  lv[s.level] = (lv[s.level] || 0) + 1; counts();
  $("ai").className = "ai " + cls;
  $("ai").innerHTML = `<div class="ph" style="margin:0;color:#fff;opacity:.8">🤖 AI 판정 · #${s.msg_id}</div><div class="lv">${ic} ${head}</div>
    <div class="ex">${esc(s.explain)}</div><div class="meta">분류 ${s.cls} · 설비 ${eqs}${s.action !== "없음" ? " · 행위 " + s.action : ""}${s.llm ? " · 🧠 LLM 교차 확인: " + esc(s.llm) : ""}</div>`;
  alarm(s.level, s);
  const to = {"A":"운전실","B":"B","C":"C"}[s.to];
  feedLine(`<span class="t">${hhmmss()}</span><span class="id">#${s.msg_id}</span><span class="w">${NAME[s.radio]}${to ? " → " + to : ""}</span>
    <span class="tx">${esc(s.text)}</span><span class="lvb" style="background:${color}33;color:${color}">${ic} ${s.level}</span>`);
  dbInsert(s.db);
  await sleep(s.level === "충돌" ? 3000 : s.level === "불일치" ? 2300 : 1500);
}
async function loop() {
  if (running) return; running = true; const g = gen;
  $("live").classList.add("on"); $("livet").textContent = "LIVE";
  while (playing && i < T.steps.length - 1) { i++; await play(T.steps[i]); if (g !== gen) return; }
  running = false;
  $("live").classList.remove("on"); $("livet").textContent = i >= T.steps.length - 1 ? "종료" : "일시정지";
  if (i >= T.steps.length - 1) { playing = false; $("play").textContent = "✔ 끝"; setTalk(null); sceneEnd(); }
}
function unlockAudio() { if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)(); }  // 클릭으로 소리 허용
$("play").onclick = () => {
  unlockAudio();
  if (!$("cover").classList.contains("hide")) { startScene(); return; }
  if (i >= T.steps.length - 1) return;
  playing = !playing; $("play").textContent = playing ? "⏸ 일시정지" : "▶ 계속";
  if (playing) loop();
};

// ── 장면 전환: 소개 카드 → 재생 → 다음 장면 ──
function resetView() {
  playing = false; running = false; i = -1; if (audio) audio.pause();
  for (const k in count) delete count[k]; for (const k in lv) delete lv[k];
  $("feed").innerHTML = ""; $("dblog").innerHTML = ""; dbCounts(); counts();
  $("play").textContent = "▶ 수신 시작"; $("live").classList.remove("on"); $("livet").textContent = "대기";
  $("ai").className = "ai"; $("ai").innerHTML = '<div class="ph" style="margin:0">🤖 AI 판정</div><div class="ex">무전 수신 대기 중입니다.</div>';
  document.querySelectorAll(".bubble").forEach(x => x.classList.remove("show")); setTalk(null);
  $("scn").textContent = T.scene;
  const here = new Set(T.steps.filter(s => s.type === "msg").map(s => s.radio));
  document.querySelectorAll(".worker").forEach(p => p.style.display = here.has(p.id.slice(1)) || here.size === 0 ? "" : "none");
  board(T.initial);
}
function radiosOf(sc) { return [...new Set(sc.steps.filter(s => s.type === "msg").map(s => s.radio))].sort(); }
function levelsOf(sc) {
  const c = {}; sc.steps.filter(s => s.type === "msg").forEach(s => c[s.level] = (c[s.level] || 0) + 1); return c;
}
function introHTML(k) {
  const sc = SC[k];
  const who = radiosOf(sc).map(r => `<span>${r === "A" ? "🎧 운전실 (A)" : "📻 작업자 " + r}</span>`).join("");
  const ini = sc.initial.map(e => {
    const lock = e.why.length > 0;
    const st = lock ? e.why.map(w => (ICON[w] || "") + " " + w.replace(" 진행 중", "")).join(" ") : "✅ 가동 가능";
    return `<div class="${lock ? "lock" : ""}"><b>${esc(e.name)}</b>${st}</div>`;
  }).join("");
  const step = SC.length > 1 ? ` · ${k + 1} / ${SC.length}` : "";
  return `<div class="k">시나리오 ${esc(sc.scene)}${step}</div><div class="t">${esc(sc.title)}</div>
    <div class="s">${esc(sc.story)}</div>
    <div class="sec">등장인물</div><div class="who">${who}</div>
    <div class="sec">시작할 때 설비 상태</div><div class="ini">${ini}</div>
    <button id="go">▶ 무전 수신 시작</button><div class="h" id="hint">🔊 녹음과 경보음이 나옵니다</div>`;
}
function showIntro(k, auto) {
  si = k; T = SC[k]; resetView();
  $("cbox").innerHTML = introHTML(k); $("cover").classList.remove("hide");
  $("go").onclick = () => { unlockAudio(); startScene(); };
  if (!auto) return;
  const g = gen; let left = INTRO_SEC;
  const tick = () => {
    if (g !== gen || $("cover").classList.contains("hide")) return;
    if (left <= 0) { startScene(); return; }
    $("go").textContent = `▶ ${left}초 후 시작`; left--; setTimeout(tick, 1000);
  };
  tick();
}
function startScene() {
  gen++; $("cover").classList.add("hide");
  playing = true; $("play").textContent = "⏸ 일시정지"; loop();
}
async function sceneEnd() {
  const g = gen; await sleep(2500); if (g !== gen) return;
  if (si < SC.length - 1) { showIntro(si + 1, true); return; }
  if (SC.length > 1) showSummary();
}
function showSummary() {
  const keys = ["충돌", "불일치", "확인 필요", "정상", "기록"];
  const rows = SC.map(sc => { const c = levelsOf(sc);
    return `<tr><td>${esc(sc.scene)} · ${esc(sc.title)}</td>${keys.map(k => `<td class="n">${c[k] || 0}</td>`).join("")}</tr>`; }).join("");
  const total = SC.reduce((n, sc) => n + sc.steps.filter(s => s.type === "msg").length, 0);
  $("cbox").innerHTML = `<div class="k">시연 결과</div><div class="t">무전 ${total}건을 듣고 판정했습니다</div>
    <table><tr><td></td>${keys.map(k => `<td class="n">${LV[k][0]}</td>`).join("")}</tr>${rows}</table>
    <div class="s">모든 교신과 판정 결과는 DB에 기록되었습니다.</div>
    <button id="again">↺ 처음부터</button>`;
  $("cover").classList.remove("hide");
  $("again").onclick = () => { gen++; showIntro(0, false); };
}
$("restart").onclick = () => { gen++; showIntro(0, false); };
dbCounts(); counts(); showIntro(0, false);
</script></body></html>
"""
