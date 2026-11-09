"""AI 복창 확인 시스템 DB 함수 모음 (SQLite)

SQL을 몰라도 DB를 쓸 수 있게, 자주 쓰는 동작을 함수로 묶었습니다.

준비
    python db/build_db.py --with-stt --classify --with-risk-data --with-risk-drafts    # DB 파일(db/hackathon.db) 만들기

사용 (저장소 맨 위 폴더에서)
    import sys; sys.path.insert(0, "db")
    import db_api

    db_api.get_state("CV-03")                          # 현재 상태
    db_api.suggest_buttons(["정비완료", "LOTO해제"])     # 무전에서 뽑은 상태 값 → 띄울 버튼
    db_api.press_button("CV-03", "LOTO_ON", msg_id=3)  # 담당자 버튼 (상태 변경 + 기록)
    db_api.reset_scene("S01")                          # 장면을 처음 상태로 되돌리기

모든 함수는 db 인자를 받습니다. 생략하면 db/hackathon.db (환경변수 HACKATHON_DB 로 바꿀 수 있음),
DB 파일 경로나 이미 열린 sqlite3 연결을 넘겨도 됩니다.

상태 규칙 (schema.sql 주석, state_actions.csv 와 같음)
    - 가동 지시의 충돌 판정은 status 가 아니라 loto_applied·permit_open 으로 한다 (get_state 의 start_blocked).
    - 작업허가 종료(PERMIT_CLOSE)는 permit_open=0 으로 바꾸고, loto_applied 가 0 일 때만 status 도 READY 로 바꾼다.
    - 상태는 press_button 으로만 바꾼다 (기록이 함께 남도록).
"""
import csv
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.environ.get("HACKATHON_DB", HERE / "hackathon.db"))
STATE_FIELDS = {"status", "loto_applied", "permit_open"}


# ───────────────────────── 내부 도우미 ─────────────────────────
@contextmanager
def _db(db=None):
    """db: None(기본 DB 파일) / 파일 경로 / 이미 열린 연결. 끝나면 저장(commit), 오류면 되돌림(rollback)."""
    if isinstance(db, sqlite3.Connection):
        con, owned = db, False
    else:
        path = Path(db) if db else DEFAULT_DB
        if not path.exists():
            raise FileNotFoundError(f"DB 파일이 없습니다: {path}\n먼저 만드세요: python db/build_db.py")
        con, owned = sqlite3.connect(path), True
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        if owned:
            con.close()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@lru_cache(maxsize=None)
def _read(name):
    with open(HERE / name, encoding="utf-8-sig", newline="") as f:
        return tuple(csv.DictReader(f))


def _actions():
    return {r["action"]: r for r in _read("state_actions.csv")}


def _mapping():
    return {r["ner_norm"]: r for r in _read("state_mapping.csv")}


def _state(con, equipment_id):
    row = con.execute(
        "SELECT e.equipment_id, e.name, e.type, s.status, s.loto_applied, s.permit_open, s.updated_at"
        " FROM equipment e JOIN equipment_state s USING (equipment_id) WHERE e.equipment_id = ?",
        (equipment_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"없는 설비입니다: {equipment_id}")
    d = dict(row)
    d["start_blocked"] = bool(d["loto_applied"] or d["permit_open"])    # 가동 지시가 오면 충돌이 되는 상태
    return d


# ───────────────────────── 상태 조회·버튼 ─────────────────────────
def get_state(equipment_id, db=None):
    """설비 하나의 현재 상태. start_blocked 가 True 면 LOTO 나 작업허가가 걸려 있어 가동 지시가 충돌이다."""
    with _db(db) as con:
        return _state(con, equipment_id)


def get_all_states(db=None):
    """모든 설비의 현재 상태 (현황판용)."""
    with _db(db) as con:
        ids = [r[0] for r in con.execute("SELECT equipment_id FROM equipment ORDER BY equipment_id")]
        return [_state(con, i) for i in ids]


def suggest_buttons(ner_values):
    """무전에서 뽑은 상태 값(NER 사전 STATE의 norm) → 화면에 띄울 버튼 목록 (state_mapping.csv 기준).

    예: ["정비완료", "LOTO해제"] → MAINT_DONE, LOTO_OFF.   '기록만'인 값은 버튼이 없다.
    같은 버튼이 두 번 나오면 한 번만 돌려준다.
    """
    actions, mapping = _actions(), _mapping()
    out, seen = [], set()
    for v in ner_values:
        row = mapping.get(v)
        if not row or not row["buttons"]:
            continue
        for code in row["buttons"].split(";"):
            if code not in seen:
                seen.add(code)
                out.append({"action": code, "button": actions[code]["button"], "kind": row["kind"], "from": v})
    return out


def press_button(equipment_id, action, msg_id=None, changed_by="담당자", db=None):
    """담당자 버튼 처리: 상태를 바꾸고 state_change_log 에 기록한다. 바뀐 뒤의 상태를 돌려준다.

    action: MAINT_START / MAINT_DONE / LOTO_ON / LOTO_OFF / PERMIT_OPEN / PERMIT_CLOSE
    msg_id: 근거가 된 무전 (없으면 None)
    """
    acts = _actions()
    if action not in acts:
        raise ValueError(f"버튼 값이 아닙니다: {action} (가능: {', '.join(acts)})")
    a = acts[action]
    if a["field"] not in STATE_FIELDS:
        raise ValueError(f"state_actions.csv 의 칸 이름이 잘못됐습니다: {a['field']}")
    with _db(db) as con:
        _state(con, equipment_id)                                   # 없는 설비면 여기서 오류
        value = a["value"] if a["field"] == "status" else int(a["value"])
        now = _now()
        con.execute(f"UPDATE equipment_state SET {a['field']} = ?, updated_at = ? WHERE equipment_id = ?",
                    (value, now, equipment_id))
        if action == "PERMIT_CLOSE":                                 # extra_rule: LOTO 가 없으면 status 도 READY
            con.execute("UPDATE equipment_state SET status = 'READY' WHERE equipment_id = ? AND loto_applied = 0",
                        (equipment_id,))
        con.execute("INSERT INTO state_change_log(equipment_id, msg_id, action, changed_at, changed_by) VALUES (?,?,?,?,?)",
                    (equipment_id, msg_id, action, now, changed_by))
        return _state(con, equipment_id)


# ───────────────────────── 장면 (시연용) ─────────────────────────
def reset_scene(scenario_id=None, clear_log=True, db=None):
    """설비 상태를 seed.sql 의 처음 상태로 되돌리고, 그 장면만 시작 상태가 다르면(예: S12) 그것을 적용한다.

    clear_log=True 면 버튼 기록(state_change_log)도 비운다. 무전·판정 기록은 건드리지 않는다.
    """
    seed = (HERE / "seed.sql").read_text(encoding="utf-8")
    insert = re.search(r"INSERT INTO equipment_state.*?;", seed, flags=re.S).group(0)
    with _db(db) as con:
        con.execute("DELETE FROM equipment_state")
        con.executescript(insert)
        for r in _read("scenes.csv"):
            if r["kind"] == "start" and r["scenario_id"] == scenario_id:
                con.execute("UPDATE equipment_state SET status = ?, loto_applied = ?, permit_open = ? WHERE equipment_id = ?",
                            (r["status"], int(r["loto_applied"]), int(r["permit_open"]), r["equipment_id"]))
        if clear_log:
            con.execute("DELETE FROM state_change_log")
    return get_all_states(db)


def scene_buttons_after(file_name):
    """이 무전 다음에 담당자가 누르는 버튼 목록 (scenes.csv). 예: 'S01_03_B.wav' → LOTO_ON, MAINT_START"""
    acts = _actions()
    return [{"equipment_id": r["equipment_id"], "action": r["action"], "button": acts[r["action"]]["button"]}
            for r in _read("scenes.csv") if r["kind"] == "event" and r["file_name"] == file_name]


# ───────────────────────── 설비 찾기·저장·조회 ─────────────────────────
def find_equipment(text, db=None):
    """문장 속 설비 별칭을 찾아 설비 번호로 바꾼다 (긴 별칭 우선, 나온 순서대로).

    예: "삼번 컨베이어 다시 가동해" → [{"equipment_id": "CV-03", "mention": "삼번 컨베이어"}]
    '5번.'처럼 번호만 있거나 '3번이랑 4번'처럼 줄여 말한 것은 찾지 못한다 (별칭에 없으므로).
    """
    with _db(db) as con:
        aliases = [(r[0], r[1]) for r in con.execute("SELECT alias, equipment_id FROM equipment_alias")]
    used, hits = [False] * len(text), []
    for alias, eid in sorted(aliases, key=lambda x: -len(x[0])):
        start = text.find(alias)
        while start != -1:
            end = start + len(alias)
            if not any(used[start:end]):
                used[start:end] = [True] * len(alias)
                hits.append((start, alias, eid))
            start = text.find(alias, start + 1)
    return [{"equipment_id": eid, "mention": alias} for _, alias, eid in sorted(hits)]


def save_message(file_name, radio_id, stt_text, scenario_id=None, addressee_id=None, call_text=None,
                 cls=None, cls_score=None, cls_rule=None, action=None, equipment=None, db=None):
    """무전 한 건을 저장하고 msg_id 를 돌려준다.

    equipment: [(설비 ID 또는 None, 들은 이름), ...]   예: [("CV-03", "삼번 컨베이어"), (None, "7번 컨베이어")]
               DB에 없는 설비는 설비 ID 를 None 으로 하고 들은 이름만 남긴다.
    """
    try:
        with _db(db) as con:
            cur = con.execute(
                "INSERT INTO radio_message(scenario_id, file_name, radio_id, addressee_id, call_text, stt_text,"
                " cls, cls_score, cls_rule, action, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scenario_id, file_name, radio_id, addressee_id, call_text, stt_text, cls, cls_score, cls_rule, action, _now()))
            msg_id = cur.lastrowid
            for eid, mention in equipment or []:
                con.execute("INSERT INTO message_equipment(msg_id, equipment_id, mention_text) VALUES (?,?,?)",
                            (msg_id, eid, mention))
            return msg_id
    except sqlite3.IntegrityError as e:
        raise ValueError(f"저장할 수 없는 값입니다: {e}") from e


def save_judgment(msg_id, level, reason=None, ref_msg_id=None, db=None):
    """판정 결과를 저장하고 judgment_id 를 돌려준다.

    level: 충돌 / 불일치 / 확인 필요 / 정상 / 기록      ref_msg_id: 비교한 원지시(복창 판정일 때)
    """
    try:
        with _db(db) as con:
            cur = con.execute("INSERT INTO judgment(msg_id, ref_msg_id, level, reason) VALUES (?,?,?,?)",
                              (msg_id, ref_msg_id, level, reason))
            return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError(f"저장할 수 없는 값입니다 (level 은 충돌/불일치/확인 필요/정상/기록만 가능): {e}") from e


def list_messages(scenario_id=None, db=None):
    """무전 목록 (이력 화면용). 설비와 가장 최근 판정이 함께 나온다. scenario_id 를 주면 그 장면만."""
    sql = (
        "SELECT m.msg_id, m.scenario_id, m.file_name, m.radio_id, m.addressee_id, m.call_text, m.stt_text,"
        " m.cls, m.cls_score, m.cls_rule, m.action,"
        " (SELECT group_concat(COALESCE(me.equipment_id, '?' || me.mention_text), ',')"
        "    FROM message_equipment me WHERE me.msg_id = m.msg_id) AS equipment,"
        " j.level AS judgment, j.reason AS judgment_reason, j.ref_msg_id"
        " FROM radio_message m"
        " LEFT JOIN judgment j ON j.judgment_id = (SELECT MAX(judgment_id) FROM judgment WHERE msg_id = m.msg_id)"
        " WHERE (? IS NULL OR m.scenario_id = ?) ORDER BY m.msg_id"
    )
    with _db(db) as con:
        return [dict(r) for r in con.execute(sql, (scenario_id, scenario_id))]
