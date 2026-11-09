"""녹음 → 텍스트(STT) → 분류(CLS) → 위험성평가 엑셀   (로컬 시연용 화면)

실행:  run.bat 더블클릭  (또는 streamlit run app.py)

1) 녹음하기 / wav 파일 올리기  → 목록에 쌓임 (재생·삭제 가능)
2) 텍스트 변환: 팀 STT (stt/src/stt.py, Whisper large-v3 + 표기 후처리)   — 틀리면 화면에서 고칠 수 있음
3) 분류: 팀 CLS (cls/src) — 목록 순서대로 앞 무전의 분류를 history 로 넘김
4) 정비요청이 있으면 위험성평가 초안 + KRAS 엑셀 (risk/src/make_draft.py). 뒤에 같은 설비 상태보고(LOTO 등)가 있으면 작업 기록에 들어감
"""
import hashlib
import importlib.util
import io
import json
import math
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from scipy.io import wavfile
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parent
for p in ("db", "cls/src", "risk/src"):
    sys.path.insert(0, str(ROOT / p))
import db_api  # noqa: E402  팀 DB 함수
import make_draft as md  # noqa: E402  위험성평가 초안
from classifier import UtteranceClassifier  # noqa: E402  팀 CLS

STT_MODEL = "openai/whisper-large-v3"        # 팀 기본 모델 (stt/src/stt.py DEFAULT_MODEL)
BASE_DB = ROOT / "data" / "rec_base.db"      # 스키마 + 설비 + 위험성평가 표 (무전 없음)
WORK_DB = ROOT / "data" / "rec_work.db"
STT_CACHE = ROOT / "data" / "stt_cache.json"   # 변환 결과 저장 (같은 파일은 다시 변환하지 않음)
SAVED_DB = ROOT / "data" / "rec_saved.db"     # 📂 목록 저장: radio_message 에 저장 이름 = scenario_id
REC_DIR = ROOT / "recordings"                 # 📂 목록 저장: 녹음 소리 recordings/<저장 이름>/<파일>
OUTPUT = ROOT / "output"
SCEN = "REC"
UNKNOWN_EQ = re.compile(r"(\d+|[일이삼사오육칠팔구십]+)\s*(번|호기)\s*(컨베이어|파쇄기|송풍기)")
CLS_ICON = {"정비요청": "🛠️", "가동지시": "▶️", "정지지시": "⏹️", "복창": "🔁", "상태보고": "📋", "기타": "💬", "불명": "❓"}

ss = st.session_state


# ───────────────────────── 준비 (한 번만)
@st.cache_resource
def stt_mod():
    """팀 stt.py (STT 클래스, postprocess)."""
    spec = importlib.util.spec_from_file_location("team_stt", ROOT / "stt" / "src" / "stt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@st.cache_resource(show_spinner="음성인식 모델 불러오는 중 (처음 한 번만, 1~2분)")
def stt_model():
    return stt_mod().STT(STT_MODEL)


def cache_read():
    try:
        return json.loads(STT_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cache_write(c):
    STT_CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


@st.cache_resource
def clf():
    return UtteranceClassifier()


@st.cache_resource
def terms():
    return md.load_terms()


def ensure_base_db():
    if not BASE_DB.exists():
        import subprocess
        subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "db" / "build_db.py"), "--out", str(BASE_DB),
                        "--with-risk-data"], check=True)


# ───────────────────────── 녹음 목록
def to_16k(data):
    """wav 바이트 → 16kHz 모노 float32 (팀 STT 입력 형식). 다른 샘플레이트·스테레오도 바꿔 준다."""
    rate, x = wavfile.read(io.BytesIO(data))
    if x.dtype == np.int16:
        x = x / 32768.0
    elif x.dtype == np.int32:
        x = x / 2147483648.0
    elif x.dtype == np.uint8:
        x = (x.astype(np.float32) - 128) / 128.0
    x = np.asarray(x, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if rate != 16000:
        g = math.gcd(rate, 16000)
        x = resample_poly(x, 16000 // g, rate // g).astype(np.float32)
    return x


def add_item(name, data):
    try:
        audio = to_16k(data)
    except Exception as e:  # noqa: BLE001
        st.toast(f"{name}: wav로 읽을 수 없습니다 ({e})", icon="⚠️")
        return
    stem, names = Path(name).stem, {Path(i["name"]).stem for i in ss.recs}
    base, k = stem, 2
    while stem in names:                      # 같은 이름이면 _2, _3 … (엑셀 파일 이름이 겹치지 않게)
        stem, k = f"{base}_{k}", k + 1
    ss.next_id += 1
    key = hashlib.sha1(data).hexdigest()
    item = {"id": ss.next_id, "name": f"{stem}.wav", "data": data, "audio": audio, "key": key,
            "sec": len(audio) / 16000, "raw": None, "stt_sec": None, "saved": False}
    hit = cache_read().get(key)
    if hit and hit["model"] == STT_MODEL:          # 💾 저장해 둔 파일이면 저장된 결과를 바로 쓴다
        item.update(raw=hit["raw"], stt_sec=hit["sec"], saved=True)
        ss[f"txt_{item['id']}"] = hit.get("text") or stt_mod().postprocess(hit["raw"])
    ss.recs.append(item)


def save_item(item_id):
    """💾 저장: Whisper 원문 + 지금 입력칸의 텍스트(고친 것 포함)를 저장. 같은 파일을 다시 올리면 바로 불러온다."""
    it = next(i for i in ss.recs if i["id"] == item_id)
    c = cache_read()
    c[it["key"]] = {"model": STT_MODEL, "name": it["name"], "raw": it["raw"], "text": ss.get(f"txt_{item_id}"),
                    "sec": round(it["stt_sec"], 1)}
    cache_write(c)
    it["saved"] = True


def unsave_item(item_id):
    it = next(i for i in ss.recs if i["id"] == item_id)
    c = cache_read()
    c.pop(it["key"], None)
    cache_write(c)
    it["saved"] = False


def reconvert(item_id):
    """🔄 다시 변환: 저장된 결과를 쓰지 않고 STT 를 새로 돌린다 (저장은 그대로, 💾 를 다시 누르면 덮어씀)."""
    it = next(i for i in ss.recs if i["id"] == item_id)
    it.update(raw=None, stt_sec=None)
    ss.pop(f"txt_{item_id}", None)
    enqueue([item_id])


def delete_item(item_id):
    ss.recs = [i for i in ss.recs if i["id"] != item_id]
    ss.queue = [q for q in ss.queue if q != item_id]
    ss.pop(f"txt_{item_id}", None)


def move_item(item_id, d):
    i = next(k for k, it in enumerate(ss.recs) if it["id"] == item_id)
    j = i + d
    if 0 <= j < len(ss.recs):
        ss.recs[i], ss.recs[j] = ss.recs[j], ss.recs[i]


def transcribe(item_id):
    item = next(i for i in ss.recs if i["id"] == item_id)
    model = stt_model()
    t0 = time.time()
    raw = model.transcribe(item["audio"][: 16000 * 30])     # Whisper는 30초까지
    item["raw"], item["stt_sec"] = raw, time.time() - t0
    ss[f"txt_{item_id}"] = stt_mod().postprocess(raw)       # 팀 후처리 (삼 번→3번, 로또→LOTO, 삐→B …)
    ss.queue = [q for q in ss.queue if q != item_id]
    ss.batch_times.append(item["stt_sec"])


# 변환 대기열: 한 번 실행에 한 건만 변환하고 화면을 다시 그린다 → 끝난 건부터 텍스트·분류·엑셀이 바로 보임
def enqueue(ids):
    new = [i for i in ids if i not in ss.queue]
    if not ss.queue:                                        # 새 묶음 시작
        ss.batch_total, ss.batch_times = 0, []
    ss.queue += new
    ss.batch_total += len(new)


def enqueue_all():
    enqueue([i["id"] for i in ss.recs if i["raw"] is None])


def stop_queue():
    ss.queue = []


def clear_all():
    for i in ss.recs:
        ss.pop(f"txt_{i['id']}", None)
    ss.recs, ss.queue = [], []


# ───────────────────────── 분류 · 위험성평가
def equipment(text):
    hits = db_api.find_equipment(text, db=str(BASE_DB))
    out = [(h["equipment_id"], h["mention"]) for h in hits]
    for m in UNKNOWN_EQ.finditer(text):       # 'N번 컨베이어' 꼴인데 DB에 없는 설비
        if not any(h["mention"] in m.group(0) or m.group(0) in h["mention"] for h in hits):
            out.append((None, m.group(0)))
    return out


def classify(items):
    """텍스트가 있는 녹음을 목록 순서대로 분류 (앞 무전 분류를 history 로)."""
    rows, history = [], []
    for it in items:
        text = (ss.get(f"txt_{it['id']}") or "").strip()
        if it["raw"] is None or not text:
            continue
        p = clf().predict(text, history)
        history.append(p["cls"])
        rows.append({"item": it, "text": text, **p, "equipment": equipment(text)})
    return rows


def risk_drafts(rows):
    """분류 결과를 시연용 DB에 넣고 정비요청마다 초안 + 엑셀. 문장·순서가 바뀔 때만 다시 만든다."""
    sig = tuple((r["item"]["name"], r["text"], r["cls"]) for r in rows)
    if ss.get("risk_sig") == sig:
        return ss.risk
    ensure_base_db()
    shutil.copyfile(BASE_DB, WORK_DB)
    for r in rows:
        db_api.save_message(r["item"]["name"], None, r["text"], scenario_id=SCEN, cls=r["cls"], cls_score=r["score"],
                            cls_rule=r["rule"], equipment=r["equipment"], db=str(WORK_DB))
    out = []
    conn = sqlite3.connect(WORK_DB)
    try:
        msgs = md.read_messages(conn, {SCEN})
        for req in (m for m in msgs if m["cls"] == "정비요청"):
            equip, note, drafts, records = md.make_draft(conn, req, msgs, terms())
            items, gaps = md.draft_items(conn, drafts, equip)
            name = conn.execute("SELECT name FROM equipment WHERE equipment_id=?", (equip,)).fetchone()
            path = OUTPUT / f"{Path(req['file']).stem}_위험성평가.xlsx"
            md.export_excel(conn, req, equip, note, drafts, records, path)
            for d in drafts:
                conn.execute("INSERT INTO risk_draft (msg_id, risk_id, status, equipment_id, created_at, excel_path)"
                             " VALUES (?,?,'DRAFT',?,datetime('now','localtime'),?)",
                             (req["msg_id"], d["risk_id"], equip, path.relative_to(ROOT).as_posix()))
            out.append({"req": req, "equip": equip, "name": name[0] if name else None, "note": note, "drafts": drafts,
                        "records": records, "items": items, "gaps": gaps, "xlsx": (path.name, path.read_bytes())})
        conn.commit()
    finally:
        conn.close()
    ss.risk_sig, ss.risk = sig, out
    return out


# ───────────────────────── 📂 목록 저장 · 불러오기 (껐다 켜도 남음)
def saved_db():
    """저장용 DB. 팀 DB와 같은 표(radio_message·message_equipment) + 시연용 표 demo_audio(소리 파일 위치·Whisper 원문)."""
    if not SAVED_DB.exists():
        ensure_base_db()
        shutil.copyfile(BASE_DB, SAVED_DB)
    con = sqlite3.connect(SAVED_DB)
    con.execute("CREATE TABLE IF NOT EXISTS demo_audio (msg_id INTEGER PRIMARY KEY REFERENCES radio_message(msg_id),"
                " wav_path TEXT, stt_raw TEXT, stt_sec REAL)")
    return con


def folder_of(name):
    return REC_DIR / (re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "이름없음")


def list_saved():
    con = saved_db()
    try:
        return con.execute("SELECT scenario_id, COUNT(*), MAX(created_at) FROM radio_message"
                           " GROUP BY scenario_id ORDER BY MAX(msg_id) DESC").fetchall()
    finally:
        con.close()


def _delete_saved(con, name):
    ids = [r[0] for r in con.execute("SELECT msg_id FROM radio_message WHERE scenario_id=?", (name,))]
    for t in ("demo_audio", "message_equipment"):
        con.executemany(f"DELETE FROM {t} WHERE msg_id=?", [(i,) for i in ids])
    con.execute("DELETE FROM radio_message WHERE scenario_id=?", (name,))
    shutil.rmtree(folder_of(name), ignore_errors=True)


def save_list():
    name = (ss.get("save_name") or "").strip()
    if not name or not ss.recs:
        return
    by_id = {r["item"]["id"]: r for r in classify(ss.recs)}
    folder = folder_of(name)
    con = saved_db()
    try:
        _delete_saved(con, name)                      # 같은 이름이면 덮어쓴다
        folder.mkdir(parents=True, exist_ok=True)
        for it in ss.recs:                            # 목록 순서 = msg_id 순서
            (folder / it["name"]).write_bytes(it["data"])
            r = by_id.get(it["id"])
            text = ss.get(f"txt_{it['id']}") if it["raw"] is not None else None
            mid = con.execute("INSERT INTO radio_message (scenario_id, file_name, stt_text, cls, cls_score, cls_rule, created_at)"
                              " VALUES (?,?,?,?,?,?,datetime('now','localtime'))",
                              (name, it["name"], text, r and r["cls"], r and r["score"], r and r["rule"])).lastrowid
            for eid, mention in (r["equipment"] if r else []):
                con.execute("INSERT INTO message_equipment (msg_id, equipment_id, mention_text) VALUES (?,?,?)",
                            (mid, eid, mention))
            con.execute("INSERT INTO demo_audio VALUES (?,?,?,?)",
                        (mid, (folder / it["name"]).relative_to(ROOT).as_posix(), it["raw"], it["stt_sec"]))
        con.commit()
    finally:
        con.close()
    c = cache_read()                                  # 변환 결과도 모두 💾 저장 (같은 파일을 다시 올려도 바로 나옴)
    for it in ss.recs:
        if it["raw"] is not None:
            c[it["key"]] = {"model": STT_MODEL, "name": it["name"], "raw": it["raw"],
                            "text": ss.get(f"txt_{it['id']}"), "sec": round(it["stt_sec"], 1)}
            it["saved"] = True
    cache_write(c)
    ss.flash = f"'{name}' 저장 완료 ({len(ss.recs)}건, 변환 결과 포함)"


def load_list():
    name = ss.get("load_pick")
    con = saved_db()
    try:
        rows = con.execute("SELECT m.file_name, m.stt_text, a.wav_path, a.stt_raw, a.stt_sec FROM radio_message m"
                           " JOIN demo_audio a USING (msg_id) WHERE m.scenario_id=? ORDER BY m.msg_id", (name,)).fetchall()
    finally:
        con.close()
    clear_all()
    cache, missing = cache_read(), []
    for fname, text, wav, raw, sec in rows:
        path = ROOT / wav
        if not path.exists():
            missing.append(fname)
            continue
        data = path.read_bytes()
        key = hashlib.sha1(data).hexdigest()
        audio = to_16k(data)
        ss.next_id += 1
        ss.recs.append({"id": ss.next_id, "name": fname, "data": data, "audio": audio, "key": key,
                        "sec": len(audio) / 16000, "raw": raw, "stt_sec": sec, "saved": key in cache})
        if raw is not None:
            ss[f"txt_{ss.next_id}"] = text or ""
    ss.flash = f"'{name}' 불러옴 ({len(ss.recs)}건)" + (f" · 소리 파일 없음: {', '.join(missing)}" if missing else "")


def delete_list():
    name = ss.get("load_pick")
    con = saved_db()
    try:
        _delete_saved(con, name)
        con.commit()
    finally:
        con.close()
    ss.flash = f"'{name}' 삭제함"


# ───────────────────────── 화면
st.set_page_config(page_title="AI 복창 확인 시연", page_icon="📻", layout="wide")
for k, v in (("recs", []), ("next_id", 0), ("rec_key", 0), ("up_key", 0), ("queue", []), ("batch_total", 0),
             ("batch_times", [])):
    ss.setdefault(k, v)

with st.sidebar:
    st.header("📂 목록 저장 · 불러오기")
    st.caption("녹음 소리·순서·고친 텍스트를 통째로 저장합니다. 껐다 켜도 남습니다. "
               "변환 결과도 모두 💾 저장돼서 같은 파일을 다시 올려도 바로 나옵니다.")
    if ss.get("flash"):
        st.success(ss.pop("flash"))
    saved = list_saved()
    names = {r[0]: r for r in saved}
    name = st.text_input("저장 이름", key="save_name", placeholder="예: S01 시연").strip()
    if name in names:
        st.caption("⚠ 같은 이름이 있어 덮어씁니다")
    st.button("💾 지금 목록 저장", on_click=save_list, disabled=not (name and ss.recs), width="stretch")
    st.divider()
    if saved:
        st.selectbox("저장된 목록", list(names), key="load_pick",
                     format_func=lambda n: f"{n} · {names[n][1]}건 · {(names[n][2] or '')[5:16]}")
        c1, c2 = st.columns(2)
        c1.button("📂 불러오기", on_click=load_list, type="primary", disabled=bool(ss.queue), width="stretch")
        with c2.popover("🗑 삭제", width="stretch"):
            st.write(f"'{ss.get('load_pick')}' 를 지울까요? 녹음 파일도 지워집니다.")
            st.button("삭제", on_click=delete_list, type="primary")
        st.caption("불러오면 지금 화면의 목록은 지워집니다 (저장 안 했으면 사라짐).")
    else:
        st.caption("저장된 목록이 없습니다.")

st.title("📻 AI 복창 확인 — 녹음에서 위험성평가까지")
st.caption("① 녹음하기·파일 올리기 → ② 텍스트 변환 (팀 STT) → ③ 분류 (CLS) → ④ 정비요청이면 위험성평가 엑셀")

# ① 녹음 추가
st.subheader("① 녹음 추가")
c1, c2 = st.columns(2)
with c1:
    rec = st.audio_input("🎙️ 녹음하기 (마이크 버튼 → 말하기 → 정지)", sample_rate=16000, key=f"rec_{ss.rec_key}")
    if rec is not None:
        add_item(f"녹음_{ss.next_id + 1:02d}.wav", rec.getvalue())
        ss.rec_key += 1
        st.rerun()
with c2:
    ups = st.file_uploader("📁 wav 파일 올리기 (여러 개 가능)", type=["wav"], accept_multiple_files=True,
                           key=f"up_{ss.up_key}")
    if ups:
        for f in ups:
            add_item(f.name, f.getvalue())
        ss.up_key += 1
        st.rerun()

# ② 녹음 목록 · 텍스트 변환 · ③ 분류
rows = classify(ss.recs)
by_id = {r["item"]["id"]: r for r in rows}
h1, h2, h3 = st.columns([4, 2, 1])
h1.subheader(f"② 텍스트 변환 · ③ 분류 ({len(ss.recs)}개)")
todo = [i["id"] for i in ss.recs if i["raw"] is None and i["id"] not in ss.queue]
if ss.queue:
    h2.button("⏹ 변환 멈추기", on_click=stop_queue, width="stretch", help="지금 변환 중인 1건은 끝까지 하고 멈춥니다")
elif todo:
    h2.button(f"📝 남은 {len(todo)}개 모두 텍스트 변환", on_click=enqueue_all, type="primary", width="stretch")
if ss.recs:
    h3.button("모두 지우기", on_click=clear_all, width="stretch")
else:
    st.info("녹음하거나 wav 파일을 올리면 여기에 순서대로 쌓입니다. 순서대로 분류합니다 (예: 정비요청 → 복창 → LOTO 보고).")
progress_box = st.empty()                                   # 진행 막대 자리 (맨 아래에서 채움)

for n, it in enumerate(ss.recs, 1):
    with st.container(border=True):
        a, b = st.columns([5, 2])
        a.markdown(f"**#{n} {it['name']}** · {it['sec']:.1f}초")
        b1, b2, b3 = b.columns(3)
        b1.button("▲", key=f"mvup_{it['id']}", on_click=move_item, args=(it["id"], -1), width="stretch", help="위로")
        b2.button("▼", key=f"mvdn_{it['id']}", on_click=move_item, args=(it["id"], 1), width="stretch", help="아래로")
        b3.button("🗑", key=f"del_{it['id']}", on_click=delete_item, args=(it["id"],), width="stretch", help="삭제")
        st.audio(it["data"], format="audio/wav")
        if it["sec"] > 30:
            st.warning("30초가 넘는 녹음은 앞 30초만 변환합니다.")
        if it["raw"] is None:
            if ss.queue and ss.queue[0] == it["id"]:
                st.markdown("⏳ **텍스트 변환 중…**")
            elif it["id"] in ss.queue:
                st.caption(f"대기 중 ({ss.queue.index(it['id'])}번째 다음)")
            else:
                st.button("📝 텍스트 변환", key=f"stt_{it['id']}", on_click=enqueue, args=([it["id"]],))
            continue
        st.text_input("텍스트 (틀렸으면 고치고 Enter → 다시 분류)", key=f"txt_{it['id']}")
        took = f"💾 저장된 결과 (처음 변환 {it['stt_sec']:.1f}초)" if it["saved"] else f"변환 {it['stt_sec']:.1f}초"
        st.caption(f"Whisper 원문: {it['raw']}  ·  {took}")
        s1, s2, s3, _ = st.columns([1, 1, 1, 3])
        s1.button("💾 다시 저장" if it["saved"] else "💾 저장", key=f"save_{it['id']}", on_click=save_item,
                  args=(it["id"],), width="stretch", help="이 결과(고친 텍스트 포함)를 저장 → 같은 파일을 다시 올리면 바로 나옴")
        s2.button("🔄 다시 변환", key=f"re_{it['id']}", on_click=reconvert, args=(it["id"],), width="stretch",
                  disabled=bool(ss.queue), help="STT를 새로 돌립니다")
        if it["saved"]:
            s3.button("저장 삭제", key=f"unsave_{it['id']}", on_click=unsave_item, args=(it["id"],), width="stretch")
        r = by_id.get(it["id"])
        if r:
            eq = ", ".join(e or f"{m}(DB에 없음)" for e, m in r["equipment"]) or "없음"
            how = (f"규칙 {r['rule']}" if r["score"] is None else
                   f"모델 확신도 {r['score']:.2f}" + ("" if r["cls"] != "불명" else f" (가장 가까운 것: {r['top']})"))
            st.markdown(f"**③ 분류: {CLS_ICON.get(r['cls'], '')} {r['cls']}**  ·  {how}  ·  설비: {eq}")

# ④ 위험성평가
st.divider()
st.subheader("④ 위험성평가 초안")
drafts = risk_drafts(rows) if rows else []
if not drafts and rows:
    st.info("분류된 무전에 정비요청이 없어 위험성평가 대상이 아닙니다. (위험성평가는 정비요청 무전에서만 만듭니다)")
if not drafts:
    st.caption("분류 결과에 정비요청이 있으면 여기에 위험성평가 초안과 엑셀이 생깁니다.  \n"
               "대본에서 정비요청인 녹음: S01_01_A · S02_01_A · S02_03_A · S02_05_A · S02_07_A · S02_09_A · "
               "S10_19_A · S10_20_A  (S07 등 지시·복창 장면에는 정비요청이 없음)")
else:
    tabs = st.tabs([Path(d["req"]["file"]).stem for d in drafts])
    for tab, d in zip(tabs, drafts):
        with tab:
            works = ", ".join(x["work"] for x in d["drafts"]) or "작업 미확인"
            equip = f"{d['name']}({d['equip']})" if d["name"] else "설비 미확인"
            st.markdown(f"**공정명:** {equip} 정비 — {works}")
            st.caption(f"요청 무전: “{d['req']['text']}”")
            rec_line = [f"작업 요청 · {Path(d['req']['file']).stem}"] + [f"{x['label']} · {x['file']}" for x in d["records"]]
            st.markdown("**작업 기록(무전):** " + "  →  ".join(rec_line))
            if d["items"]:
                df = pd.DataFrame([{"세부작업": s[0], "분류": s[1], "위험요인": s[2], "감소대책": s[3],
                                    "가능성": s[4] or "", "중대성": s[5] or "", "위험성": s[6] or ""} for s in d["items"]])
                st.dataframe(df, hide_index=True, width="stretch",
                             column_config={"위험요인": st.column_config.TextColumn(width="large"),
                                            "감소대책": st.column_config.TextColumn(width="large")})
                st.caption(f"본문 {len(d['items'])}행 · 가능성·중대성·위험성은 담당자가 입력 (빈칸)")
            for g in ([d["note"]] if d["note"] else []) + d["gaps"]:
                st.warning(g)
            fname, data = d["xlsx"]
            st.download_button(f"📥 엑셀 받기 ({fname})", data, file_name=fname, key=f"dl_{fname}", type="primary",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ───────────────────────── 대기열 처리: 화면을 다 그린 뒤 한 건 변환 → 다시 그리기
if ss.queue:
    cur = next(i for i in ss.recs if i["id"] == ss.queue[0])
    done = ss.batch_total - len(ss.queue)
    with progress_box.container(border=True):
        st.progress(done / ss.batch_total if ss.batch_total else 0,
                    text=f"텍스트 변환 {done} / {ss.batch_total} 완료 · 지금: {cur['name']}")
        if ss.batch_times:
            avg = sum(ss.batch_times) / len(ss.batch_times)
            st.caption(f"이 PC 평균 1건 {avg:.0f}초 · 남은 {len(ss.queue)}건 약 {avg * len(ss.queue) / 60:.1f}분 예상")
        else:
            st.caption("첫 건이 끝나면 남은 시간을 계산합니다 (앱을 켜고 처음이면 모델 불러오기 20초쯤 더)")
        with st.spinner(f"{cur['name']} 변환 중…"):
            transcribe(cur["id"])
    st.rerun()
