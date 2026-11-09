"""팀 CLS 규칙 사전(teamfile/Hackerthon/dictionary/cls_dictionary.csv)을 코드로 옮긴 규칙 엔진.

판단 순서
  1 기타 키워드 → 2 정지지시 키워드 → 3 상태보고 → 4 복창
  → 7 이어받기 → 5 조합(정비요청 / 가동지시 / 정지지시)
  → 어떤 규칙에도 안 걸리면 None (분류기가 모델로 판단)
- 7을 5보다 먼저 보는 이유: 사전 예시 "가동 아니고 정지 → 정지지시"처럼 정정 문장에는
  행위가 둘 들어 있어서, 5로 먼저 판단하면 틀린다.
- 우선순위 1~4, 6의 단어 목록은 CSV에서 읽는다. 팀이 단어를 추가하면 코드 수정 없이 반영된다.
  조건 문구(condition)는 아래 _cond()에서 해석한다.
- 단어 판정(설비·행위·작업·상태)은 NER 사전(ner_dictionary.csv)을 직접 조회한다.
- 사용자 결정으로 추가한 것: '가동 중지'는 '가동 금지'와 똑같이 정지지시, '중지'는 정지(확정사항.md).
"""
import csv
import re

from common import CLS_DICT, NER_DICT, ORDER_LABELS

GADONG = {"가동", "리셋"}                               # 확정사항: 리셋 = 가동 쪽
JEONGJI = {"정지", "정지유지", "가동금지", "가동취소"}
EXTRA_P2 = ["가동 중지"]
EXTRA_ACTION = [("가동 중지", "가동금지"), ("중지", "정지")]
NUM_EQUIP = re.compile(r"(?:\d+|[일이삼사오육칠팔구])\s*(?:번|호기|호|컨)")
# 사전 보완(팀에 제안 예정) — 사전 취지와 확정사항 R7에 맞춘 것
NOT_BOKCHANG_HAMNIDA = ["요청합니다", "부탁합니다"]            # '합니다' 복창 판정에서 뺌 (사전 note: 요청합니다 제외)
NEG_GADONG = re.compile(r"(?:가동|돌리|켜|기동|살리)\S*?\s*(?:하)?지\s*마|(?:가동하|돌리|켜)면\s*안")   # 부정된 가동 → 정지지시


def ns(s):
    return re.sub(r"\s+", "", s)


def compile_term(term):
    """공백 유무에 상관없이 맞춘다. 두 글자 이하 단어는 앞이 띄어쓰기일 때만 인정('그런'의 '런' 오탐 방지)."""
    core = r"\s*".join(map(re.escape, ns(term)))
    return re.compile((r"(?:(?<=\s)|^)" if len(ns(term)) <= 2 else "") + core)


class RuleEngine:
    def __init__(self):
        self.rows = {1: [], 2: [], 3: [], 4: []}
        self.order_end, self.req_end = [], []
        with open(CLS_DICT, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                p, pat = int(r["priority"]), r["pattern"].strip()
                if p in self.rows:
                    self.rows[p].append((pat, r["condition"].strip(), r["cls"]))
                elif p == 6 and r["cls"] == "(지시)" and "명사로 끝남" not in pat:
                    self.order_end.append(ns(pat))
                elif p == 6 and r["cls"] == "(요청)":
                    self.req_end.append(ns(pat))
        self.rows[2] += [(x, "", "정지지시") for x in EXTRA_P2]

        self.terms = {"EQUIP": [], "EQUIP_TYPE": [], "ACTION": [], "TASK": [], "STATE": []}
        with open(NER_DICT, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["label"] in self.terms and "문맥 확인" not in (r.get("note") or ""):
                    self.terms[r["label"]].append((compile_term(r["term"]), r["norm"]))
        self.terms["ACTION"] += [(compile_term(t), n) for t, n in EXTRA_ACTION]

    # ---------- 단어 찾기
    def _spans(self, kind, text):
        """kind 단어가 나온 위치 목록 [(시작, 끝, 정규값)] — 더 긴 단어 안에 든 짧은 단어는 뺀다."""
        found = [(m.start(), m.end(), norm) for rx, norm in self.terms[kind] for m in rx.finditer(text)]
        return [a for a in found if not any(b != a and b[0] <= a[0] and a[1] <= b[1] and b[1] - b[0] > a[1] - a[0]
                                            for b in found)]

    def has(self, kind, text):
        return bool(self._spans(kind, text))

    def action_family(self, text):
        """마지막에 나온 행위가 가동 쪽이면 '가동', 정지 쪽이면 '정지'."""
        spans = sorted(self._spans("ACTION", text))
        for _, _, norm in reversed(spans):
            if norm in GADONG:
                return "가동"
            if norm in JEONGJI:
                return "정지"
        return None

    def equip_mentioned(self, text):
        return self.has("EQUIP", text) or self.has("EQUIP_TYPE", text) or bool(NUM_EQUIP.search(text))

    def _match(self, pat, text):
        return ns(pat) in ns(text)

    def _cond(self, cond, text):
        if not cond:
            return True
        if cond == "설비 번호 없음":
            return not NUM_EQUIP.search(text.replace("몇 번", "").replace("몇번", ""))
        if cond.startswith("STATE"):
            return self.has("STATE", text)
        if cond == "있는 설비가 나옴":
            return self.has("EQUIP", text)
        if cond.startswith("설비 + 행위"):
            return self.equip_mentioned(text) and (self.has("ACTION", text) or self.has("TASK", text))
        return False   # 모르는 조건은 적용하지 않음

    def _ends_order(self, text):
        """지시 어미로 끝나거나(우선순위 6), 행위 명사로 끝나면('1호 파쇄기 정지.') 지시로 본다."""
        t = text.rstrip()
        if any(ns(t).endswith(p) for p in self.order_end):
            return True
        return any(end == len(t) for _, end, _ in self._spans("ACTION", t))

    def _ends_request(self, text):
        return any(ns(text).endswith(p) for p in self.req_end)

    # ---------- 우선순위별 판단
    def apply(self, text, history=()):
        t = text.replace("?", " ").strip()
        last = next((h for h in reversed(list(history)) if h in ORDER_LABELS), None)

        for pat, cond, _ in self.rows[1]:
            if self._match(pat, t) and self._cond(cond, t):
                return "기타", f"P1 기타: {pat}"
        for pat, _, _ in self.rows[2]:
            if self._match(pat, t):
                return "정지지시", f"P2 정지지시: {pat}"
        if NEG_GADONG.search(t):
            return "정지지시", "P2(보완) 부정된 가동"
        for pat, cond, _ in self.rows[3]:
            if self._match(pat, t) and self._cond(cond, t):
                return "상태보고", f"P3 상태보고: {pat}"
        for pat, cond, _ in self.rows[4]:
            body = t
            if pat == "합니다":
                for x in NOT_BOKCHANG_HAMNIDA:
                    body = body.replace(x, "")
            if not self._match(pat, body):
                continue
            if self._cond(cond, body):
                return "복창", f"P4 복창: {pat}"
            if pat == "말씀이십니까" or (pat in ("하겠습니다", "겠습니다") and not self.equip_mentioned(body)):
                return "기타", f"P4 설비 없음 → 기타: {pat}"

        r = self._inherit(t, last)
        if r:
            return r

        has_task = self.has("TASK", t)
        fam = self.action_family(t)
        order, req = self._ends_order(t), self._ends_request(t)
        if has_task and order:
            return "정비요청", "P5 작업+지시 어미 (작업 우선)"
        if fam == "가동" and (order or req):
            return "가동지시", "P5 가동+" + ("지시" if order else "요청") + " 어미"
        if fam == "정지" and order:
            return "정지지시", "P5 정지+지시 어미"
        return None

    def _inherit(self, t, last):
        """우선순위 7: 정정('아니 / 아니고 / 아니라'), '응', 설비·번호만 있는 조각."""
        s = re.sub(r"^(?:[ABC]|운전실)\s+", "", t)
        if re.search(r"아니고|아니라|^아니(?:\s|$)", s):
            tail = re.split(r"아니고|아니라|아니", s)[-1]
            fam = self.action_family(tail)
            if fam:
                return ("가동지시" if fam == "가동" else "정지지시"), f"P7 정정+행위({fam})"
            if self.has("TASK", tail):
                return "정비요청", "P7 정정+작업"
            if last:
                return last, "P7 정정 → 직전 지시 이어받음"
            return None
        if last and re.match(r"^응(?:\s|$)", s):
            return last, "P7 '응' → 직전 지시 이어받음"
        if (last and self.equip_mentioned(s) and not self.has("ACTION", s) and not self.has("TASK", s)
                and not ns(self._strip_equip(s))):
            return last, "P7 설비·번호만 → 직전 지시 이어받음"
        return None

    def _strip_equip(self, text):
        """설비 이름·번호를 지운 나머지 ('5번.' → '', '2번 블로워 끄고 대기' → '끄고 대기')."""
        spans = self._spans("EQUIP", text) + self._spans("EQUIP_TYPE", text) + \
            [(m.start(), m.end(), "") for m in NUM_EQUIP.finditer(text)]
        out = list(text)
        for s, e, _ in spans:
            for i in range(s, e):
                out[i] = " "
        return "".join(out)
