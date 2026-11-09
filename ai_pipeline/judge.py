"""판정 엔진: 무전 한 건(CLS + 설비·행위)과 DB 설비 상태로 🔴🟠🟡🟢⚪를 정한다.

판정 기준 (radio_script.md 라벨 정의)
  🔴 충돌     LOTO·정비 중·작업허가 설비에 가동 지시(요청 포함), 또는 그 지시의 복창
  🟠 불일치   복창의 설비·행위가 원지시와 다름
  🟡 확인 필요 설비 없음·미지정·번호 불명, 원지시가 모호, 분류 불명, 오인식 가능 단어
  🟢 정상     충돌 없음 / 복창 일치
  ⚪ 기록     상태보고·기타, 되묻는 복창(내용 일치)

복창 짝짓기
  받는 무전기가 분명하면(“B,”) 그 무전기에 한 지시와, 사람 이름으로 불러 모르면(불명)
  가장 최근 지시와 비교한다. 설비가 겹치는 옛 지시를 고르는 방식은 쓰지 않는다:
  잘못된 복창이 옛 지시와 우연히 맞아 🟠를 놓칠 수 있기 때문 (놓치는 것보다 한 번 더 묻는 게 안전).
"""
from datetime import datetime, timedelta

import db
from llm_post import ORDER_CLS, family

LEVEL_ICON = {"충돌": "🔴", "불일치": "🟠", "확인 필요": "🟡", "정상": "🟢", "기록": "⚪"}
RECENT = 6  # 복창 짝을 찾을 때 볼 최근 교신 수


class Judge:
    def __init__(self, conn):
        self.conn = conn
        self.log = []  # 판정한 무전 (dict: msg_id, radio, cls, equipment_ids, action, level, ts …)

    def reset(self):
        self.log = []

    def judge(self, m):
        """m: msg_id, radio, cls, equipment_ids, equipment_mentions, action, addressee,
        is_question, uncertain, uncertain_reason → (level, reason, ref_msg_id)"""
        cls = m["cls"]
        if cls == "가동지시":
            res = self._run_order(m)
        elif cls in ("정지지시", "정비요청"):
            res = self._other_order(m)
        elif cls == "복창":
            res = self._readback(m)
        elif cls == "상태보고":
            res = ("기록", "상태보고 기록 (설비 상태는 담당자 버튼으로 변경)", None)
        elif cls == "기타":
            res = ("기록", "판정 대상 아님", None)
        else:
            res = ("확인 필요", f"발화 분류 불명 ({cls})", None)
        self.log.append({**m, "level": res[0], "ts": m.get("ts") or datetime.now()})
        return res

    # ── 지시 ──────────────────────────────────────────

    def _issue(self, m):
        """설비를 확실히 알 수 없는 이유. 없으면 None."""
        if m["equipment_mentions"]:
            return f"등록 안 된 설비: {', '.join(m['equipment_mentions'])}"
        if not m["equipment_ids"]:
            return m["uncertain_reason"] or "설비를 알아듣지 못함"
        if m["uncertain"]:
            return m["uncertain_reason"]
        return None

    def _blocked(self, ids):
        out = {}
        for eid in ids:
            why = db.blocked_reasons(self.conn, eid)
            if why:
                out[eid] = why
        return out

    def _run_order(self, m):
        blocked = self._blocked(m["equipment_ids"])
        if blocked:  # 설비가 여럿이면 하나라도 막혀 있으면 충돌
            detail = "; ".join(f"{eid} {', '.join(why)}" for eid, why in blocked.items())
            when = " (요청 단계 사전 경고)" if m["radio"] != "A" else ""
            return "충돌", f"{detail} → 가동 지시 충돌{when}", None
        # 번호를 못 알아들었어도 추정 설비가 잠겨 있으면 먼저 경고한다 (놓치는 것보다 안전)
        guessed = self._blocked(m.get("equipment_guess", [])) if not m["equipment_ids"] else {}
        if guessed:
            detail = "; ".join(f"{eid} {', '.join(why)}" for eid, why in guessed.items())
            return "충돌", f"설비 번호 불명확, 직전 교신 기준 {detail}(추정) → 가동 지시 충돌 가능, 즉시 확인", None
        issue = self._issue(m)
        if issue:
            return "확인 필요", issue, None
        return "정상", "충돌 없음 · 복창 대기", None

    def _other_order(self, m):
        issue = self._issue(m)
        if issue:
            return "확인 필요", issue, None
        what = "정지 지시" if m["cls"] == "정지지시" else "정비요청 기록"
        return "정상", f"{what} · 복창 대기", None

    # ── 복창 ──────────────────────────────────────────

    def _find_order(self, m):
        allowed = {m["radio"], "불명", "없음"}
        for h in reversed(self.log[-RECENT:]):
            if h["cls"] in ORDER_CLS and h["radio"] != m["radio"] and h["addressee"] in allowed:
                return h
        return None

    def _readback(self, m):
        ref = self._find_order(m)
        if ref is None:
            return "확인 필요", "비교할 원지시 없음", None
        rid = ref["msg_id"]
        said = set(m["equipment_ids"]) | set(m["equipment_mentions"])
        if ref["level"] == "확인 필요":
            heard = f" (복창 기준 {', '.join(sorted(said))})" if said else ""
            return "확인 필요", f"원지시가 불명확해 지시자 확인 필요{heard}", rid

        # 원지시 번호를 못 들었으면 추정 설비와 비교
        ordered = (set(ref["equipment_ids"]) | set(ref["equipment_mentions"])) or set(ref.get("equipment_guess", []))
        if not said:
            return "확인 필요", "복창에서 설비를 알아듣지 못함", rid

        diffs = []
        if said != ordered:
            diffs.append(f"설비: 지시 {','.join(sorted(ordered)) or '-'} ↔ 복창 {','.join(sorted(said))}")
        fo, fr = family(ref["action"]), family(m["action"])
        if fo and fr and fo != fr:
            diffs.append(f"행위: 지시 {ref['action']} ↔ 복창 {m['action']}")
        if diffs:
            return "불일치", " / ".join(diffs), rid

        if "가동" in (fo, fr):
            blocked = self._blocked(m["equipment_ids"])
            if blocked:
                detail = "; ".join(f"{eid} {', '.join(why)}" for eid, why in blocked.items())
                return "충돌", f"복창 일치, 충돌 유지 ({detail})", rid
        if m["is_question"]:
            return "기록", "되묻는 복창 (내용 일치) · 지시자 답 대기", rid
        if m["uncertain"]:
            return "확인 필요", f"복창 일치하나 {m['uncertain_reason']}", rid
        return "정상", "복창 일치", rid

    # ── 복창 없음 (실시간 운용용) ─────────────────────

    def missing_readbacks(self, now=None, timeout=15):
        """정상으로 판정된 지시 중 timeout초 안에 복창이 없던 것. 시연 화면에서 주기적으로 호출."""
        now = now or datetime.now()
        out = []
        for i, h in enumerate(self.log):
            if h["cls"] not in ORDER_CLS or h["level"] != "정상" or h.get("no_readback_flagged"):
                continue
            answered = any(x["cls"] == "복창" and x["radio"] != h["radio"] for x in self.log[i + 1:])
            if not answered and now - h["ts"] > timedelta(seconds=timeout):
                h["no_readback_flagged"] = True
                out.append(h)
        return out
