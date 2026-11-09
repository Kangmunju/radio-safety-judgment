"""판정 로직 (규칙 기반). db_api 위에서 동작하며, 무전 한 건마다 충돌/불일치/확인 필요/정상/기록 을 정한다.

사용 (저장소 맨 위 폴더에서)
    import sys; sys.path.insert(0, "db")
    import judge
    judge.judge_scene("S01")      # 장면 하나를 처음부터 끝까지 판정하고 DB(judgment 표)에 저장
    judge.judge_all()             # 모든 장면
    python db/judge.py            # 같은 일을 명령으로 (--scene S01, --db 경로, --no-save)

규칙 (대본 radio_script.md 의 '예상 판정'과 같음)
    기록        상태보고·기타 (잡담, 되묻기 질문 포함)
    정상        정비요청 / 정지 지시 (LOTO 설비라도 충돌 아님) / 막힌 것 없는 가동 지시 / 지시와 같은 복창
    충돌        가동 지시(요청·시운전 포함)인데 그 설비에 LOTO 또는 작업허가가 걸려 있음 (get_state 의 start_blocked)
                충돌 지시에 대한 복창이 일치해도 충돌 유지
    불일치      복창의 설비 번호나 행위(가동/정지)가 원지시와 다름 (없는 설비로 복창한 경우 포함)
    확인 필요   지시의 설비가 없음·모호·DB에 없음 / 원지시가 모호한데 복창이 옴 / 원지시 없이 복창이 옴
    정정        "아니, 가동." "가동 아니고 정지." 처럼 설비 없이 정정하면 직전 지시의 설비를 이어받는다.

설비 상태는 장면 안에서 담당자 버튼(scenes.csv)을 누른 순서대로 바뀌므로, 무전을 순서대로 읽으며
각 무전 직전의 상태로 판정한다 (judge_scene 이 reset_scene 과 버튼 누르기를 대신 한다).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_api  # noqa: E402

ORDER_LABELS = {"가동지시": "start", "정지지시": "stop"}
STOP_WORDS = ("정지", "멈", "세우", "세워", "스톱", "꺼", "대기")
START_WORDS = ("가동", "기동", "돌", "켜", "시운전", "스타트", "런", "살려")
QUESTION = re.compile(r"(말씀이십니까|입니까|습니까)")
CORRECTION = re.compile(r"(^\s*아니[\s,.]|아니고|아니라)")


def _direction(msg):
    """무전의 행위 방향: 'start' / 'stop' / None. 지시는 분류 값, 복창은 action 칸이나 문장에서 읽는다."""
    if msg["cls"] in ORDER_LABELS:
        return ORDER_LABELS[msg["cls"]]
    text = f"{msg.get('action') or ''} {msg.get('stt_text') or ''}"
    if any(w in text for w in STOP_WORDS):
        return "stop"
    if any(w in text for w in START_WORDS):
        return "start"
    return None


def _equipment(msg):
    """무전의 설비 목록. DB에 없는 설비는 '?들은이름' 으로 나온다."""
    return [e for e in (msg.get("equipment") or "").split(",") if e]


def _fmt(ids):
    return ", ".join(i.lstrip("?") + ("(없는 설비)" if i.startswith("?") else "") for i in ids) or "없음"


def judge_message(msg, pending, get_state):
    """무전 한 건의 판정 → (level, reason, ref_msg_id, 새로 기억할 원지시 or None)

    msg:        db_api.list_messages 가 돌려주는 한 줄
    pending:    직전 지시 {'msg_id','equipment','direction','ambiguous','conflict','cls'} 또는 None
    get_state:  설비 ID → db_api.get_state 결과 (충돌 판정용)
    """
    cls, ids, direction = msg["cls"], _equipment(msg), _direction(msg)

    if cls == "정비요청":
        order = dict(msg_id=msg["msg_id"], equipment=set(ids), direction=None, ambiguous=not ids or any(i.startswith("?") for i in ids),
                     conflict=False, cls=cls)
        if order["ambiguous"]:
            return "확인 필요", "정비 요청의 설비를 알 수 없음", None, order
        return "정상", "정비 요청 (위험성 평가 초안 대상)", None, order

    if cls in ORDER_LABELS:
        if not ids and pending and CORRECTION.search(msg["stt_text"] or ""):
            ids = sorted(pending["equipment"])                       # 정정: 직전 지시의 설비를 이어받는다
            inherited = True
        else:
            inherited = False
        order = dict(msg_id=msg["msg_id"], equipment=set(ids), direction=direction, ambiguous=False, conflict=False, cls=cls)
        if not ids:
            order["ambiguous"] = True
            return "확인 필요", "지시한 설비가 없거나 모호함 (그 컨베이어, 저쪽 파쇄기 등)", None, order
        unknown = [i for i in ids if i.startswith("?")]
        if unknown:
            order["ambiguous"] = True
            return "확인 필요", f"DB에 없는 설비: {_fmt(unknown)}", None, order
        suffix = " (정정 — 직전 지시의 설비를 이어받음)" if inherited else ""
        if direction == "start":
            blocked = [i for i in ids if get_state(i)["start_blocked"]]
            if blocked:
                why = []
                for i in blocked:
                    s = get_state(i)
                    why.append(f"{i} " + ("+".join(n for n, on in (("LOTO", s["loto_applied"]), ("작업허가 중", s["permit_open"])) if on)))
                order["conflict"] = True
                return "충돌", "가동 지시인데 " + ", ".join(why) + suffix, None, order
        return "정상", f"{_fmt(ids)} {'가동' if direction == 'start' else '정지'} 지시, 충돌 없음, 복창 대기{suffix}", None, order

    if cls == "복창":
        if pending is None:
            return "확인 필요", "복창인데 비교할 원지시가 없음", None, None
        ref = pending["msg_id"]
        if pending["ambiguous"]:
            return "확인 필요", "원지시가 모호해 복창을 확인할 수 없음 (지시자 확인 필요)", ref, None
        want, got = pending["equipment"], set(ids)
        if got and got != want:
            return "불일치", f"지시 {_fmt(sorted(want))} ↔ 복창 {_fmt(ids)}", ref, None
        if direction and pending["direction"] and direction != pending["direction"]:
            names = {"start": "가동", "stop": "정지"}
            return "불일치", f"지시 {names[pending['direction']]} ↔ 복창 {names[direction]}", ref, None
        if direction is None and QUESTION.search(msg["stt_text"] or ""):
            return "기록", "되묻는 확인 질문 (복창 아님)", ref, None
        if pending["conflict"]:
            return "충돌", "복창은 일치하지만 충돌 상태가 유지됨", ref, None
        return "정상", "복창 일치", ref, None

    return "기록", "상태보고·기타는 기록만 남김", None, None


def judge_scene(scenario_id, db=None, save=True, apply_buttons=True):
    """장면 하나를 순서대로 판정한다. 돌려주는 값: [(msg 한 줄, level, reason), ...]

    apply_buttons=True 면 reset_scene 으로 시작 상태를 맞추고, 무전마다 담당자 버튼(scenes.csv)을 누른 상태로 진행한다.
    save=True 면 그 무전의 기존 판정을 지우고 새로 저장한다 (judge.py 가 만든 값이므로 다시 계산 가능).
    """
    if apply_buttons:
        db_api.reset_scene(scenario_id, db=db)
    pending, results = None, []
    for msg in db_api.list_messages(scenario_id, db=db):
        level, reason, ref, order = judge_message(msg, pending, lambda i: db_api.get_state(i, db=db))
        if order is not None:
            pending = order
        if save:
            with db_api._db(db) as con:
                con.execute("DELETE FROM judgment WHERE msg_id = ?", (msg["msg_id"],))
            db_api.save_judgment(msg["msg_id"], level, reason=reason, ref_msg_id=ref, db=db)
        if apply_buttons:
            for b in db_api.scene_buttons_after(msg["file_name"]):
                db_api.press_button(b["equipment_id"], b["action"], msg_id=msg["msg_id"], db=db)
        results.append((msg, level, reason))
    return results


def judge_all(db=None, save=True, apply_buttons=True):
    """모든 장면을 판정한다."""
    scenes = sorted({m["scenario_id"] for m in db_api.list_messages(db=db) if m["scenario_id"]})
    out = []
    for s in scenes:
        out += judge_scene(s, db=db, save=save, apply_buttons=apply_buttons)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="무전 판정 실행")
    ap.add_argument("--scene", help="장면 하나만 (예: S01). 없으면 전체")
    ap.add_argument("--db", help="DB 파일 경로 (기본 db/hackathon.db)")
    ap.add_argument("--no-save", action="store_true", help="저장하지 않고 화면에만 출력")
    a = ap.parse_args()
    res = judge_scene(a.scene, db=a.db, save=not a.no_save) if a.scene else judge_all(db=a.db, save=not a.no_save)
    icons = {"충돌": "🔴", "불일치": "🟠", "확인 필요": "🟡", "정상": "🟢", "기록": "⚪"}
    for m, level, reason in res:
        print(f"{m['file_name']:<14} {icons[level]} {level:<5} {m['stt_text'][:28]:<30} {reason}")
