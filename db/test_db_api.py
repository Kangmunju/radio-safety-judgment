"""db_api.py 시험. 임시 DB를 새로 만들어서 대본의 상태 변화와 함수 동작을 확인한다.

실행:  python db/test_db_api.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db_api  # noqa: E402

ok_count, failed = 0, []


def check(title, cond, detail=""):
    global ok_count
    if cond:
        ok_count += 1
        print(f"  통과  {title}")
    else:
        failed.append(title)
        print(f"  실패  {title}  {detail}")


def raises(func, *args, **kw):
    try:
        func(*args, **kw)
    except ValueError:
        return True
    return False


def st(db, eid):
    s = db_api.get_state(eid, db=db)
    return s["status"], s["loto_applied"], s["permit_open"]


def press_scene_buttons(db, file_name):
    for b in db_api.scene_buttons_after(file_name):
        db_api.press_button(b["equipment_id"], b["action"], db=db)


def main():
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    subprocess.run([sys.executable, str(HERE / "build_db.py"), "--out", str(tmp), "--with-stt"],
                   check=True, capture_output=True)
    db = str(tmp)

    print("① 처음 상태")
    states = db_api.get_all_states(db=db)
    check("설비 8개", len(states) == 8, len(states))
    check("CV-04: LOTO 적용 → 가동 지시가 충돌", db_api.get_state("CV-04", db=db)["start_blocked"])
    check("FN-02: 작업허가 중 → 가동 지시가 충돌", db_api.get_state("FN-02", db=db)["start_blocked"])
    check("CV-01: 걸린 것 없음", not db_api.get_state("CV-01", db=db)["start_blocked"])

    print("② 버튼 안내 (번역표)")
    codes = lambda v: [b["action"] for b in db_api.suggest_buttons(v)]
    check("LOTO적용 → LOTO_ON, MAINT_START", codes(["LOTO적용"]) == ["LOTO_ON", "MAINT_START"], codes(["LOTO적용"]))
    check("정비완료+LOTO해제 → MAINT_DONE, LOTO_OFF", codes(["정비완료", "LOTO해제"]) == ["MAINT_DONE", "LOTO_OFF"])
    check("작업허가종료 → PERMIT_CLOSE", codes(["작업허가종료"]) == ["PERMIT_CLOSE"])
    check("기록만인 값(이상, 사람있음)은 버튼 없음", codes(["이상", "사람있음"]) == [])
    check("같은 버튼은 한 번만", codes(["작업허가종료", "작업완료"]) == ["PERMIT_CLOSE"])

    print("③ 대본 S01: LOTO 걸고 정비하고 푸는 흐름")
    db_api.reset_scene("S01", db=db)
    check("시작: 운전 가능", st(db, "CV-03") == ("READY", 0, 0))
    press_scene_buttons(db, "S01_03_B.wav")
    check("#3 뒤: 정비 중 + LOTO → #4 가동 지시는 충돌", st(db, "CV-03") == ("MAINTENANCE", 1, 0)
          and db_api.get_state("CV-03", db=db)["start_blocked"])
    press_scene_buttons(db, "S01_08_B.wav")
    check("#8 뒤: 운전 가능 → #9 가동 지시는 정상", st(db, "CV-03") == ("READY", 0, 0)
          and not db_api.get_state("CV-03", db=db)["start_blocked"])

    print("④ 대본 S05: 작업허가 종료")
    db_api.reset_scene("S05", db=db)
    check("시작: 정비 중, 작업허가 중", st(db, "FN-02") == ("MAINTENANCE", 0, 1))
    press_scene_buttons(db, "S05_03_C.wav")
    check("#35 뒤: 운전 가능 → #36 가동 지시는 정상", st(db, "FN-02") == ("READY", 0, 0))

    print("⑤ 대본 S12: 시작 상태가 다른 장면")
    db_api.reset_scene("S12", db=db)
    check("CV-03이 정비 중 + LOTO 로 시작", st(db, "CV-03") == ("MAINTENANCE", 1, 0))
    db_api.reset_scene("S01", db=db)
    check("다른 장면으로 되돌리면 기본 상태", st(db, "CV-03") == ("READY", 0, 0))

    print("⑥ 규칙과 오류")
    db_api.press_button("CV-04", "PERMIT_OPEN", db=db)
    db_api.press_button("CV-04", "PERMIT_CLOSE", db=db)
    check("LOTO 가 걸린 설비는 작업허가를 종료해도 정비 중으로 남음", st(db, "CV-04") == ("MAINTENANCE", 1, 0))
    check("잘못된 버튼 값은 오류", raises(db_api.press_button, "CV-03", "LOTO적용", db=db))
    check("없는 설비는 오류", raises(db_api.press_button, "CV-99", "LOTO_ON", db=db))
    db_api.reset_scene("S01", db=db)
    import sqlite3
    con = sqlite3.connect(db)
    check("장면을 되돌리면 버튼 기록도 비움", con.execute("SELECT COUNT(*) FROM state_change_log").fetchone()[0] == 0)
    db_api.press_button("CV-03", "LOTO_ON", msg_id=3, db=db)
    row = con.execute("SELECT equipment_id, msg_id, action, changed_by FROM state_change_log").fetchone()
    check("버튼을 누르면 근거 무전과 함께 기록됨", row == ("CV-03", 3, "LOTO_ON", "담당자"), row)
    con.close()

    print("⑥-2 이미 열린 연결도 받는다")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    check("연결을 넘겨도 동작", db_api.get_state("CV-01", db=con)["equipment_id"] == "CV-01")
    con.close()

    print("⑦ 설비 찾기")
    ids = lambda t: [h["equipment_id"] for h in db_api.find_equipment(t, db=db)]
    check("삼번 컨베이어 → CV-03", ids("창우야 삼번 컨베이어 다시 가동해줘.") == ["CV-03"])
    check("2번 송풍기 → FN-02 (2번 컨베이어와 구분)", ids("2번 송풍기 기동합니다.") == ["FN-02"])
    check("두 설비는 나온 순서대로", ids("5컨 세우고 1번 컨베이어 돌려") == ["CV-05", "CV-01"], ids("5컨 세우고 1번 컨베이어 돌려"))
    check("없는 설비(9번)는 못 찾음", ids("9번 컨베이어 가동해 줘") == [])

    print("⑧ 무전·판정 저장과 조회")
    m1 = db_api.save_message("T_01_A.wav", "A", "B 3번 컨베이어 가동해 줘", scenario_id="T", cls="가동지시",
                             equipment=[("CV-03", "3번 컨베이어")], db=db)
    m2 = db_api.save_message("T_02_B.wav", "B", "7번 컨베이어 가동합니다", scenario_id="T", cls="복창",
                             equipment=[(None, "7번 컨베이어")], db=db)
    db_api.save_judgment(m1, "정상", db=db)
    db_api.save_judgment(m2, "불일치", reason="지시 3번 ↔ 복창 7번", ref_msg_id=m1, db=db)
    rows = {r["file_name"]: r for r in db_api.list_messages("T", db=db)}
    check("무전 2건이 장면별로 조회됨", set(rows) == {"T_01_A.wav", "T_02_B.wav"})
    check("설비와 판정이 함께 나옴", rows["T_01_A.wav"]["equipment"] == "CV-03" and rows["T_01_A.wav"]["judgment"] == "정상")
    check("없는 설비는 '?들은이름'으로 표시", rows["T_02_B.wav"]["equipment"] == "?7번 컨베이어", rows["T_02_B.wav"]["equipment"])
    check("복창 판정에 비교한 원지시가 연결됨", rows["T_02_B.wav"]["ref_msg_id"] == m1)
    check("잘못된 판정 값은 오류", raises(db_api.save_judgment, m1, "엉뚱함", db=db))
    check("잘못된 분류 값은 오류", raises(db_api.save_message, "x.wav", "A", "x", cls="엉뚱함", db=db))

    print(f"\n통과 {ok_count}개, 실패 {len(failed)}개")
    if failed:
        print("실패 목록:", failed)
        sys.exit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
