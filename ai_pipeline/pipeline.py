"""전체 파이프라인: STT 결과 → 팀 CLS → 설비·행위 추출 → DB 저장 → 판정 → 채점.

    STT 결과(stt_results.csv, stt.py)
      → 텍스트 교정 (llm_post.clean_text)
      → 발화 분류 (팀 CLS: Hackerthon/cls)
      → 설비·행위 추출 (llm_post.Extractor, 규칙 + Qwen 보조)
      → radio.db 저장 (팀 DB 구조)
      → 판정 (judge.Judge: 🔴🟠🟡🟢⚪)

장면(S01, S02 …)마다 설비 상태를 초기값으로 되돌리고, 대본에서 담당자가 버튼을 누르는
지점(상태보고 뒤)은 SCENARIO_BUTTONS로 흉내 낸다. 끝나면 대본의 예상 판정과 비교해 채점한다.

사용법 (팀 CLS가 scikit-learn을 쓰므로 그게 깔린 환경에서):
    python pipeline.py                     # 규칙 + Qwen 교차 확인 (기본, 모든 문장)
    python pipeline.py --llm need          # 규칙이 모를 때만 Qwen (빠름)
    python pipeline.py --llm off           # 규칙만
    python pipeline.py --scene S01         # 장면 하나만
"""
import argparse
import csv
import re
from collections import Counter
from pathlib import Path

import db
from judge import LEVEL_ICON, Judge
from llm_post import DEFAULT_MODEL, LLM_MODES, Extractor, clean_text, read_csv
from team_cls import cls_text, load_classifier

BASE = Path(__file__).resolve().parent
SCRIPT_MD = BASE / "radio_script.md"

# 대본에서 상태보고 뒤 담당자가 누르는 버튼 — 팀 DB의 scenes.csv
SCENARIO_BUTTONS = db.scene_buttons()
EMOJI_LEVEL = {"🔴": "충돌", "🟠": "불일치", "🟡": "확인 필요", "🟢": "정상", "⚪": "기록"}


def new_state():
    """장면 하나 동안 유지하는 교신 기록 (이어받기·복창 짝짓기에 씀)."""
    return {"history": [], "cls_hist": [], "file_of": {}}


def process_message(conn, clf, ext, judge, state, scene, file, radio, raw):
    """무전 한 건: 교정 → CLS → 추출 → DB 저장 → 판정. pipeline.py와 app.py가 같이 쓴다."""
    p = clf.predict(cls_text(clean_text(raw)), state["cls_hist"])
    e = ext.extract(radio, raw, p["cls"], state["history"])
    return save_and_judge(conn, judge, state, scene, file, radio, raw, p["cls"], e, p.get("score"), p.get("rule"))


def row_to_extraction(r):
    """llm_results.csv 한 줄 → 추출 결과 dict (Extractor.extract가 돌려주는 모양)."""
    split = lambda s: [x for x in (s or "").split(",") if x]  # noqa: E731
    return {
        "corrected_text": r["corrected_text"], "equipment_ids": split(r.get("equipment_ids")),
        "equipment_mentions": split(r.get("equipment_mentions")), "equipment_guess": split(r.get("equipment_guess")),
        "action": r.get("action") or "없음", "tasks": split(r.get("tasks")), "addressee": r.get("addressee") or "없음",
        "inherited": r.get("inherited") == "True", "is_question": r.get("is_question") == "True",
        "eq_type": r.get("eq_type") or None, "uncertain": r.get("uncertain") == "True",
        "uncertain_reason": r.get("uncertain_reason", ""), "source": r.get("source", ""),
        "llm_raw": r.get("llm_raw", ""), "llm_check": r.get("llm_check", ""),
    }


def process_precomputed(conn, judge, state, scene, file, radio, r):
    """미리 돌린 llm_results.csv 결과로 무전 한 건 처리: 모델 없이 DB 저장 → 판정만."""
    return save_and_judge(conn, judge, state, scene, file, radio, r["stt_text"], r["cls"], row_to_extraction(r))


def save_and_judge(conn, judge, state, scene, file, radio, raw, cls, e, cls_score=None, cls_rule=None):
    state["cls_hist"].append(cls)
    state["history"].append({**e, "cls": cls, "radio": radio})
    # stt_text에는 교정한 문장을 넣는다 (팀 build_db.py도 교정본 hyp_post를 넣음)
    msg_id = db.save_message(
        conn, scenario_id=scene, file_name=file, radio_id=radio, addressee_id=e["addressee"],
        stt_text=e["corrected_text"], cls=cls, action=e["action"], cls_score=cls_score, cls_rule=cls_rule,
        equipment_ids=e["equipment_ids"], mentions=e["equipment_mentions"], guess=e["equipment_guess"])
    state["file_of"][msg_id] = file
    level, reason, ref = judge.judge({**e, "msg_id": msg_id, "radio": radio, "cls": cls})
    db.save_judgment(conn, msg_id, ref, level, reason)
    return {**e, "msg_id": msg_id, "file": file, "radio": radio, "stt_text": raw, "cls": cls,
            "level": level, "reason": reason, "ref_file": state["file_of"].get(ref, "")}


def load_gold():
    """대본 표에서 파일별 정답 CLS와 예상 판정(첫 번째 이모지)."""
    gold = {}
    if not SCRIPT_MD.exists():  # 원본 대본(예상 판정 포함)이 없으면 채점하지 않는다
        return gold
    for line in SCRIPT_MD.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\|\s*\d+\s*\|\s*S\d+_\d+_[A-Z]\.wav", line):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        level = next((lv for e, lv in EMOJI_LEVEL.items() if cols[7].startswith(e)), None)
        gold[cols[1]] = {"cls": cols[4], "level": level, "expected": cols[7]}
    return gold


def score(rows):
    rows = [r for r in rows if r["gold_level"]]
    n = len(rows)
    ok = sum(r["level"] == r["gold_level"] for r in rows)
    cls_ok = sum(r["cls"] == r["gold_cls"] for r in rows)
    print("\n── 채점 (radio_script.md 예상 판정 기준) ──")
    print(f"판정 정확도 : {ok}/{n} ({ok / max(n, 1):.1%})")
    print(f"CLS 정확도  : {cls_ok}/{n} ({cls_ok / max(n, 1):.1%})")

    # 안전 관점에서 가장 중요한 숫자: 충돌을 놓치지 않았는가
    want = [r for r in rows if r["gold_level"] == "충돌"]
    caught = sum(r["level"] == "충돌" for r in want)
    caught_or_warn = sum(r["level"] in ("충돌", "확인 필요") for r in want)
    false_alarm = sum(r["level"] == "충돌" and r["gold_level"] != "충돌" for r in rows)
    print(f"🔴 충돌 탐지 : {caught}/{len(want)}  (🟡로라도 경고: {caught_or_warn}/{len(want)}), 오경보 {false_alarm}건")
    want = [r for r in rows if r["gold_level"] == "불일치"]
    print(f"🟠 불일치 탐지: {sum(r['level'] == '불일치' for r in want)}/{len(want)}")

    print("\n예상 → 결과 (건수)")
    for (g, p), c in sorted(Counter((r["gold_level"], r["level"]) for r in rows).items()):
        mark = "  " if g == p else "✗ "
        print(f"  {mark}{LEVEL_ICON[g]} {g:<5} → {LEVEL_ICON.get(p, '?')} {p:<5} {c}")

    wrong = [r for r in rows if r["level"] != r["gold_level"]]
    if wrong:
        print("\n틀린 판정:")
        for r in wrong:
            print(f"  {r['file']:<14} {LEVEL_ICON.get(r['level'], '?')}{r['level']}(예상 {r['gold_level']}) "
                  f"| {r['corrected_text']} | {r['reason']}")


def main():
    p = argparse.ArgumentParser(description="무전 안전 확인 파이프라인")
    p.add_argument("--input", default=str(BASE / "stt_results.csv"))
    p.add_argument("--text-col", default="hyp")
    p.add_argument("--output", default=str(BASE / "pipeline_results.csv"))
    p.add_argument("--db", default=str(db.DB_PATH))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--llm", choices=list(LLM_MODES), default="all",
                   help="all: 모든 문장 교차 확인(기본) / need: 규칙이 모를 때만 / off: 규칙만")
    p.add_argument("--rules-only", action="store_true", help="--llm off 와 같음")
    p.add_argument("--from-llm-results", metavar="CSV", nargs="?", const=str(BASE / "llm_results.csv"),
                   help="미리 돌린 llm_post.py 결과로 판정만 (모델 안 씀, 빠름). 경로 생략 시 llm_results.csv")
    p.add_argument("--scene", help="이 장면만 (예: S01)")
    p.add_argument("--no-eval", action="store_true")
    args = p.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)
    pre = args.from_llm_results
    if pre:
        print(f"LLM 결과 파일로 판정: {pre} (CLS·추출은 파일 값 사용)")
        clf = ext = None
    else:
        clf = load_classifier()
        ext = Extractor(model=args.model, mode="off" if args.rules_only else args.llm)
    judge = Judge(conn)
    gold = load_gold()

    rows = sorted((r for r in read_csv(pre or args.input) if not r.get("error")), key=lambda r: r["file"])
    if args.scene:
        rows = [r for r in rows if r["file"].startswith(args.scene.upper())]

    out, scene, state = [], None, new_state()
    for r in rows:
        file, raw = r["file"], (r["stt_text"] if pre else r[args.text_col])
        radio = re.search(r"_([A-Z])\.wav$", file).group(1)
        if file[:3] != scene:  # 장면마다 설비 상태·교신 기록 초기화
            scene, state = file[:3], new_state()
            db.reset_state(conn, scene)
            judge.reset()
            print(f"\n── {scene} ──")

        if pre:
            m = process_precomputed(conn, judge, state, scene, file, radio, r)
        else:
            m = process_message(conn, clf, ext, judge, state, scene, file, radio, raw)
        g = gold.get(file, {})
        print(f"{LEVEL_ICON[m['level']]} {file:<14} [{radio}] {m['cls']:<5} "
              f"{','.join(m['equipment_ids']) or '-':<12} | {m['corrected_text']}  → {m['reason']}")
        out.append({
            "file": file, "radio": radio, "stt_text": raw, "corrected_text": m["corrected_text"], "cls": m["cls"],
            "equipment_ids": ",".join(m["equipment_ids"]), "equipment_mentions": ",".join(m["equipment_mentions"]),
            "equipment_guess": ",".join(m["equipment_guess"]), "action": m["action"], "addressee": m["addressee"], "level": m["level"], "reason": m["reason"],
            "ref_file": m["ref_file"], "source": m["source"],
            "gold_cls": g.get("cls", ""), "gold_level": g.get("level", ""), "expected": g.get("expected", ""),
        })

        for eid, action in SCENARIO_BUTTONS.get(file, []):  # 담당자 버튼 흉내
            db.press_button(conn, eid, action, by="담당자(시나리오)", msg_id=m["msg_id"])
            print(f"   ⏺ 담당자 버튼: {eid} {db.BUTTON_LABEL[action]}")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"\n저장: {args.output}, DB: {args.db}  (LLM 호출 {ext.llm_calls if ext else '파일 사용'}건)")
    if not args.no_eval and gold:  # radio_script.md 가 없으면 채점 생략
        score(out)


if __name__ == "__main__":
    main()
