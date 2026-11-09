"""팀 DB(GitHub Hackerthon/db)를 그대로 쓰는 연결부.

표 구조·초기 데이터·버튼·장면은 모두 팀 저장소 파일을 쓴다:
    db/schema.sql, db/seed.sql, db/risk_data.sql   표와 초기 데이터
    db/db_api.py                                   상태 조회·버튼·저장 함수 (실제 동작은 여기서)
    db/scenes.csv                                  장면 시작 상태(S12)와 대본상 담당자 버튼
    db/state_actions.csv                           버튼 6개 정의
이 파일은 판정·파이프라인·화면이 부르기 쉽게 이름만 맞춰 둔 얇은 껍데기다.

팀 규칙: 가동 지시 충돌은 status(정비 중)가 아니라 loto_applied·permit_open으로 판단한다.

사용법:
    python db.py --init                    # radio.db 새로 만들기 (팀 schema.sql + seed.sql + risk_data.sql)
    python db.py --show                    # 설비 상태판
    python db.py --button CV-03 LOTO_ON    # 담당자 버튼
"""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from team_cls import TEAM_REPO

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "radio.db"              # 팀 저장소의 db/hackathon.db는 건드리지 않는다
TEAM_DB_DIR = TEAM_REPO / "db"
if not (TEAM_DB_DIR / "db_api.py").exists():
    raise FileNotFoundError(f"팀 DB 폴더가 없습니다: {TEAM_DB_DIR} (git pull 또는 HACKATHON_REPO 확인)")
if str(TEAM_DB_DIR) not in sys.path:
    sys.path.append(str(TEAM_DB_DIR))  # 맨 뒤: 팀 db/judge.py 가 우리 judge.py 를 가리지 않게
import db_api  # noqa: E402  팀 DB 함수 모음

TABLES = ["risk_draft", "risk_step", "risk_template", "judgment", "state_change_log",
          "message_equipment", "radio_message", "equipment_alias", "equipment_state", "equipment"]


def _read(name):
    with open(TEAM_DB_DIR / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


BUTTON_LABEL = {r["action"]: r["button"] for r in _read("state_actions.csv")}  # LOTO_ON → "LOTO 적용"
BUTTONS = list(BUTTON_LABEL)


def scene_buttons():
    """대본상 담당자가 누르는 버튼: {무전 파일: [(설비, 버튼), …]} (scenes.csv)."""
    out = {}
    for r in _read("scenes.csv"):
        if r["kind"] == "event":
            out.setdefault(r["file_name"], []).append((r["equipment_id"], r["action"]))
    return out


# ── 연결·생성 ─────────────────────────────────────────

def connect(path=DB_PATH):
    conn = sqlite3.connect(path, check_same_thread=False)  # Streamlit에서도 쓰도록
    conn.row_factory = sqlite3.Row                          # db_api가 dict(row)를 쓴다
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    """팀 schema.sql + seed.sql (+ risk_data.sql)로 새로 만든다. 기존 기록은 지운다."""
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((TEAM_DB_DIR / "schema.sql").read_text(encoding="utf-8"))
    conn.executescript((TEAM_DB_DIR / "seed.sql").read_text(encoding="utf-8"))
    risk = TEAM_DB_DIR / "risk_data.sql"
    if risk.exists():
        conn.executescript(risk.read_text(encoding="utf-8"))
    conn.commit()


def reset_state(conn, scene=None):
    """설비 상태를 처음 상태로 + 장면 시작 상태(scenes.csv). 무전·판정·버튼 기록은 남긴다."""
    db_api.reset_scene(scene, clear_log=False, db=conn)


# ── 조회 ──────────────────────────────────────────────

def blocked_reasons(conn, eid):
    """가동하면 안 되는 이유. 비어 있으면 가동 가능. (팀 규칙: LOTO·작업허가 기준)"""
    try:
        s = db_api.get_state(eid, db=conn)
    except ValueError:
        return ["등록 안 된 설비"]
    if not s["start_blocked"]:
        return []
    return ((["정비 중"] if s["status"] == "MAINTENANCE" else [])
            + (["LOTO 적용"] if s["loto_applied"] else [])
            + (["작업허가 진행 중"] if s["permit_open"] else []))


def board(conn):
    return db_api.get_all_states(db=conn)


# ── 기록 ──────────────────────────────────────────────

def save_message(conn, *, scenario_id, file_name, radio_id, addressee_id, stt_text, cls, action,
                 equipment_ids, mentions, guess=(), cls_score=None, cls_rule=None):
    """무전 한 건 저장 → msg_id. 없는 설비는 (NULL, 들은 이름), 추정 설비는 (설비, '추정')으로 남긴다."""
    equipment = ([(e, None) for e in equipment_ids] + [(None, m) for m in mentions]
                 + [(e, "추정") for e in guess if e not in equipment_ids])
    return db_api.save_message(
        file_name, radio_id, stt_text, scenario_id=scenario_id,
        addressee_id=addressee_id if addressee_id in ("A", "B", "C") else None,
        cls=cls, cls_score=cls_score, cls_rule=cls_rule,
        action=None if action == "없음" else action, equipment=equipment, db=conn)


def save_judgment(conn, msg_id, ref_msg_id, level, reason):
    db_api.save_judgment(msg_id, level, reason, ref_msg_id, db=conn)


def press_button(conn, eid, action, by="담당자", msg_id=None):
    """담당자 버튼. 설비 상태는 이것으로만 바뀐다 (팀 db_api.press_button, 기록 포함)."""
    db_api.press_button(eid, action, msg_id=msg_id, changed_by=by, db=conn)


def print_board(conn):
    print("── 설비 상태 ──")
    for b in board(conn):
        why = blocked_reasons(conn, b["equipment_id"])
        print(f"{b['equipment_id']}  {b['name']:<8} " + (f"🔴 가동 금지 ({', '.join(why)})" if why else "🟢 가동 가능"))


def main():
    p = argparse.ArgumentParser(description="설비 상태·무전 기록 DB (팀 DB 구조)")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--init", action="store_true", help="DB 새로 만들기 (기존 기록 삭제)")
    p.add_argument("--button", nargs=2, metavar=("설비ID", "버튼"), help=f"버튼: {', '.join(BUTTONS)}")
    p.add_argument("--show", action="store_true", help="설비 상태판")
    args = p.parse_args()

    conn = connect(args.db)
    if args.init:
        init_db(conn)
        print(f"초기화: {args.db}")
    if args.button:
        press_button(conn, args.button[0].upper(), args.button[1].upper(), by="CLI")
        print(f"{args.button[0].upper()}: {BUTTON_LABEL[args.button[1].upper()]}")
    print_board(conn)


if __name__ == "__main__":
    main()
