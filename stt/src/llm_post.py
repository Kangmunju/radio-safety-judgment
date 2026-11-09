"""설비·행위 추출 (규칙 + LLM 보조). 발화 분류(CLS)는 팀 분류기가 하고, 여기서는 하지 않는다.

입력: Whisper 원문 + 팀 CLS 결과 + 같은 채널의 직전 교신
출력: 교정 텍스트, 설비 ID, 들은 설비 표현(없는 설비 포함), 행위, 작업, 받는 무전기, 이어받기 여부, 확인 필요 사유

1) 규칙: stt.py의 postprocess + ner_dictionary.csv로 먼저 뽑는다.
2) LLM(Qwen, 로컬): 규칙이 빈칸을 못 채운 문장만 다시 해석한다.
   예) 가동지시인데 행위를 못 찾음, 복창인데 설비를 못 찾음, 가동·정지가 함께 나옴
3) 검증: LLM이 고른 설비가 등록 설비인지, 실제로 들린 번호인지 코드로 다시 확인한다.

다른 코드에서 쓸 때:
    from llm_post import Extractor, clean_text
    ext = Extractor(use_llm=True)
    ext.extract("A", "삼 번 컨베이어 다시 가동해 줘", cls="가동지시", history=[])

단독 실행 (팀 CLS를 같이 불러서 추출 결과와 설비 정확도를 본다):
    python llm_post.py --rules-only
    python llm_post.py
"""
import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
NER_CSV = BASE / "ner_dictionary.csv"
SCRIPT_MD = BASE / "radio_script.md"

DEFAULT_MODEL = "Qwen/Qwen2.5-3B-Instruct"
HISTORY_SIZE = 4

# 설비 마스터 (팀 DB 초기 데이터와 같음)
EQUIPMENT = {
    "CV-01": "1번 컨베이어", "CV-02": "2번 컨베이어", "CV-03": "3번 컨베이어",
    "CV-04": "4번 컨베이어", "CV-05": "5번 컨베이어",
    "CR-01": "1호기 파쇄기", "CR-02": "2호기 파쇄기",
    "FN-02": "2번 송풍기",
}
TYPE_NAME = {"CV": "컨베이어", "CR": "파쇄기", "FN": "송풍기"}
TYPE_WORD = {"컨베이어": "CV", "컨": "CV", "벨트": "CV", "파쇄기": "CR", "크러셔": "CR",
             "송풍기": "FN", "블로워": "FN"}
TYPE_RE = "컨베이어|컨|벨트|파쇄기|크러셔|송풍기|블로워"  # 긴 것부터

ORDER_CLS = {"정비요청", "가동지시", "정지지시"}
NO_ACTION_CLS = {"상태보고", "기타"}
ACTION_VALUES = ["가동", "정지", "정지유지", "가동금지", "가동취소", "리셋", "없음"]
RUN_FAMILY = ["가동", "리셋"]
STOP_FAMILY = ["가동금지", "가동취소", "정지유지", "정지"]
KOR_DIGIT = {1: "일", 2: "이", 3: "삼", 4: "사", 5: "오", 6: "육", 7: "칠", 8: "팔", 9: "구"}

TEXT_FIXES = [
    (re.compile(r"[컴펀건큰]베이어"), "컨베이어"),
    (re.compile(r"낙강"), "낙광"),
    (re.compile(r"^\s*([ABC])\s*(?=\d)"), r"\1, "),               # large-v3: "B3번 컨베이어" → "B, 3번 컨베이어"
    (re.compile(r"^\s*[가-힣]{1,3}[야아]\s*,?\s+"), "[호출] "),  # 사람 이름 호출은 남기지 않음
    (re.compile(r"^\s*[가-힣]{1,3}어\s*,?\s+(?=\d|[일이삼사오육칠팔구]\s*번)"), "[호출] "),  # "유니어 7번…" (번호 앞일 때만)
]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def compact(text):
    return re.sub(r"[\s.,?!]", "", text)


def family(action):
    if action in RUN_FAMILY:
        return "가동"
    if action in STOP_FAMILY:
        return "정지"
    return None


# ── 사전 ──────────────────────────────────────────────

class Lexicon:
    def __init__(self, path=NER_CSV):
        self.ner = defaultdict(list)
        for r in read_csv(path):
            r["key"] = compact(r["term"])
            r["note"] = r.get("note") or ""
            self.ner[r["label"]].append(r)
        for rows in self.ner.values():
            rows.sort(key=lambda r: -len(r["key"]))

    def hints(self, text, limit=25):
        """LLM 프롬프트용: 문장과 글자가 겹치는 사전 줄만 (작은 모델은 긴 프롬프트에 약함)."""
        s = compact(text)
        grams = {s[i:i + 2] for i in range(len(s) - 1)}
        out = []
        for label in ("EQUIP", "ACTION", "TASK"):
            for r in self.ner[label]:
                k = r["key"]
                if any(k[i:i + 2] in grams for i in range(max(len(k) - 1, 1))):
                    out.append(f"{r['term']} → {r['norm']}")  # "EQUIP:CV-02" 꼴로 주면 Qwen이 그대로 베껴 씀
        return out[:limit]


def find_terms(text, rows):
    """공백 무시, 긴 표현부터, 찾은 자리는 가려서 겹침 방지. 한 글자 표현(켜, 꺼, 런)은 어절 맨 앞만."""
    masked = compact(text)
    found = []
    for r in rows:
        k = r["key"]
        if len(k) <= 1:
            if re.search(rf"(?:^|\s){re.escape(r['term'])}(?=$|[\s.,?!]|줘)", text):
                found.append(r)
            continue
        pos = masked.find(k)
        while pos >= 0:
            found.append(r)
            masked = masked[:pos] + "#" * len(k) + masked[pos + len(k):]
            pos = masked.find(k)
    return found, masked


# ── 규칙 추출 ─────────────────────────────────────────

_DICT_FIXES = None


def dict_fixes():
    """사전의 'STT 오인식' 표현 → 바른 표기 ("이루기 파쇄기" → "1호기 파쇄기", "룰러" → "롤러").
    문맥 확인이 필요한 표현("사건", "가능합니다")은 글자를 바꾸지 않고 확인 필요로만 남긴다."""
    global _DICT_FIXES
    if _DICT_FIXES is None:
        fixes = []
        rows = read_csv(NER_CSV)
        # 작업어는 정규값("막힘제거")이 띄어쓰기가 없어 어색하므로, 같은 정규값의 대본 표현("막힘 제거")으로 바꾼다
        script_term = {(r["label"], r["norm"]): r["term"] for r in rows if r["source"] == "대본"}
        for r in rows:
            note = r.get("note") or ""
            if r["source"] != "STT 오인식" or "문맥" in note or "모호" in note:
                continue
            if r["label"] == "EQUIP" and r["norm"] in EQUIPMENT:
                rep = EQUIPMENT[r["norm"]]
            elif r["label"] == "PART":
                rep = r["norm"]
            elif r["label"] == "TASK" and (r["label"], r["norm"]) in script_term:
                rep = script_term[(r["label"], r["norm"])]
            else:
                continue  # 상태 정규값 등은 문장에 그대로 넣기 어색해서 뺌
            pat = re.compile(r"\s*".join(map(re.escape, compact(r["term"]))))
            fixes.append((len(compact(r["term"])), pat, rep))
        _DICT_FIXES = [(p, rep) for _, p, rep in sorted(fixes, key=lambda x: -x[0])]
    return _DICT_FIXES


# (패턴, 바꿀 말, 확인 필요 사유) — 설비 이름·행위어가 바로 붙어 있을 때만 고친다
CONTEXT_FIXES = [
    (re.compile(r"(컨베이어|파쇄기|송풍기|블로워|크러셔)(\s*)가능합니다"), r"\1\2가동합니다",
     "STT '가능합니다'를 '가동합니다'로 보정 (오인식 의심)"),
    (re.compile(r"(?<![가-힣])사건(?=\s*(재?기동|재?가동|돌려|세워|정지|스톱))"), "4컨",
     "STT '사건'을 '4컨'으로 보정 (오인식 의심)"),
]


def context_fix_notes(text):
    return [note for pat, _, note in CONTEXT_FIXES if pat.search(text)]


def clean_text(raw):
    """오타 고치기 + stt.py 후처리 + 사전의 STT 오인식 표기 고치기. CLS에 넣을 문장도 이걸로 만든다."""
    from stt import postprocess  # torch를 미리 올리지 않도록 여기서 가져온다
    text = raw
    for pat, rep in TEXT_FIXES:
        text = pat.sub(rep, text)
    text = postprocess(text)
    for pat, rep_, _ in CONTEXT_FIXES:
        text = pat.sub(rep_, text)
    for pat, rep in dict_fixes():
        text = pat.sub(rep, text)
    return text


def extract_equipment(text, lex):
    found, masked = find_terms(text, lex.ner["EQUIP"])
    items, unknown, flags = [], [], []
    for r in found:
        if "문맥" in r["note"] or "모호" in r["note"]:
            flags.append(f"'{r['term']}'은(는) 오인식일 수 있음")
        items.append(r["norm"])

    def take(m):  # 사전에 없는 "N번 컨베이어 / N호기 파쇄기" (없는 설비 포함)
        n, prefix = int(m.group("num")), TYPE_WORD[m.group("type")]
        eid = f"{prefix}-{n:02d}"
        (items if eid in EQUIPMENT else unknown).append(eid if eid in EQUIPMENT else f"{n}번 {TYPE_NAME[prefix]}")
        return "#" * len(m.group(0))

    masked = re.sub(rf"(?P<num>\d+)(번|호기|호)(라인)?(?P<type>{TYPE_RE})", take, masked)
    nums = [int(n) for n in re.findall(r"(\d+)번(?!째)", masked)]                  # 번호만 ("5번.")
    types = [TYPE_WORD[t] for t in re.findall(TYPE_RE, masked) if t != "컨"]      # 종류만 ("그 컨베이어")
    if re.search(rf"(?:^|[^\d#몇])(?<!몇 )번\s*({TYPE_RE})", text):                  # "B, 번 컨베이어" ("몇 번"은 제외)
        flags.append("설비 번호가 들리지 않음")
    return {"ids": list(dict.fromkeys(items)), "unknown": unknown, "nums": nums, "types": types, "flags": flags}


def extract_actions(text, lex):
    found, _ = find_terms(text, lex.ner["ACTION"])
    acts = list(dict.fromkeys(r["norm"] for r in found))
    flags = [f"'{r['term']}'은(는) 오인식일 수 있음" for r in found if "문맥" in r["note"]]
    run = [a for a in RUN_FAMILY if a in acts]
    stop = [a for a in STOP_FAMILY if a in acts]
    if run and stop:
        return None, flags  # 가동과 정지가 함께 → 규칙으로 못 정함
    return (stop or run or ["없음"])[0], flags


CORRECTION_RE = re.compile(r"아니\s*고|아니\s*라")


def is_followup(text):
    t = re.sub(r"^\s*(\[호출\]|[ABC],)\s*", "", text)
    return bool(CORRECTION_RE.search(t) or re.match(r"^\s*(아니|응)\b", t))


def is_question(text):
    return bool(re.search(r"(말씀이십니까|습니까|니까)\s*\??\s*$|\?\s*$", text.strip()))


def annotate_guess(text, guess, number_unclear):
    """STT가 뭉갠 번호만 문장에 채운다: "B, 번 컨베이어" → "B, 3번 컨베이어".
    "그 컨베이어"처럼 처음부터 번호를 말하지 않은 문장은 그대로 둔다 (말하지 않은 걸 문장에 넣지 않음).
    추정이라는 사실은 equipment_guess · uncertain · uncertain_reason 칸에 남는다."""
    if not guess or not number_unclear:
        return text
    names = ", ".join(EQUIPMENT[e] for e in guess)
    return re.sub(rf"(?<![\d몇])(?<!몇 )번\s*({TYPE_RE})", names, text, count=1)


def backfill_addressee(history, radio, cls):
    """이름으로 부른 지시(받는 무전기 '불명')는 그 지시에 처음 답한 현장 무전기를 받는 쪽으로 본다.
    고친 history 위치를 돌려준다 (없으면 None)."""
    if radio == "A" or cls not in ("복창", "상태보고", "기타"):
        return None
    for k in range(len(history) - 1, -1, -1):
        h = history[k]
        if h["radio"] == "A" and h["cls"] in ORDER_CLS:
            if h["addressee"] == "불명":
                h["addressee"] = radio
                return k
            return None
    return None


def backfill_from_readback(history, radio, cls, result):
    """복창에 설비가 나왔는데 그 원지시는 번호를 못 들었던 경우, 원지시에 '복창 기준 추정'을 붙인다.
    고친 history 위치를 돌려준다 (없으면 None). 원지시 판정은 바꾸지 않는다 — 표시와 기록용."""
    if cls != "복창" or not result["equipment_ids"]:
        return None
    for k in range(len(history) - 1, -1, -1):
        h = history[k]
        if h["cls"] in ORDER_CLS and h["radio"] != radio:
            if not h["equipment_ids"] and not h.get("equipment_guess") and \
                    "설비 번호가 들리지 않음" in h.get("uncertain_reason", ""):
                h["equipment_guess"] = list(result["equipment_ids"])
                h["eq_type"] = h["equipment_guess"][0][:2]
                h["corrected_text"] = annotate_guess(h["corrected_text"], h["equipment_guess"], True)
                names = ", ".join(EQUIPMENT[e] for e in h["equipment_guess"])
                h["uncertain_reason"] = f"{h['uncertain_reason']} / 복창 기준 {names}로 추정"
                return k
            return None
    return None


def last_order(history):
    return next((h for h in reversed(history) if h["cls"] in ORDER_CLS), None)


def last_type(history):
    return next((h["eq_type"] for h in reversed(history) if h.get("eq_type")), None)


def addressee(text, radio, cls):
    m = re.match(r"^\s*([ABC])\s*,", text)
    if m:
        return m.group(1)
    if text.lstrip().startswith("[호출]"):
        return "불명"
    if text.lstrip().startswith("운전실") or (radio != "A" and cls in ("복창", "상태보고")):
        return "A"
    return "없음"


def rule_extract(radio, raw, cls, history, lex):
    text = clean_text(raw)
    need_llm, hard = [], []  # need_llm: LLM에 물어볼 이유 / hard: LLM과 상관없이 확인 필요

    m = CORRECTION_RE.search(text)          # 정정 문장은 "아니고/아니라" 뒤가 진짜 내용
    focus = text[m.end():] if m else text

    eq = extract_equipment(focus, lex)
    action, act_flags = extract_actions(focus, lex)
    if action is None:
        need_llm.append("가동과 정지가 함께 나옴")
        action = "없음"
    tasks = list(dict.fromkeys(r["norm"] for r in find_terms(text, lex.ner["TASK"])[0]))
    parts = list(dict.fromkeys(r["norm"] for r in find_terms(text, lex.ner["PART"])[0]))
    hard += eq["flags"] + act_flags
    from stt import postprocess as _pp  # 보정 전 문장 기준으로 사유 확인
    hard += context_fix_notes(_pp(raw))

    # 이어받기: 지시로 분류됐는데 정정·짧은 답이거나 설비/번호만 있는 문장
    inherited = False
    prev = last_order(history)
    only_equipment = action == "없음" and not tasks and bool(eq["ids"] or eq["nums"])
    if cls in ORDER_CLS and prev and (is_followup(text) or only_equipment):
        inherited = True
        if action == "없음":
            action = prev["action"]
        if not (eq["ids"] or eq["nums"]):
            eq["ids"] = list(prev["equipment_ids"])
            eq["unknown"] = eq["unknown"] or list(prev.get("equipment_mentions", []))

    # 번호만 나온 경우: 같은 문장 → 직전 교신 순으로 설비 종류를 찾는다
    if eq["nums"]:
        prefix = eq["types"][0] if eq["types"] else eq["ids"][0][:2] if eq["ids"] else last_type(history)
        if prefix:
            for n in eq["nums"]:
                eid = f"{prefix}-{n:02d}"
                if eid in EQUIPMENT:
                    if eid not in eq["ids"]:
                        eq["ids"].append(eid)
                else:
                    eq["unknown"].append(f"{n}번 {TYPE_NAME[prefix]}")
        else:
            need_llm.append("설비 종류를 알 수 없음")

    if eq["unknown"]:
        hard.append(f"등록 안 된 설비: {', '.join(dict.fromkeys(eq['unknown']))}")
    unclear = "설비 번호가 들리지 않음" in hard or bool(eq["types"] and not eq["ids"] and not eq["unknown"] and not inherited)
    if eq["types"] and not eq["ids"] and not eq["unknown"] and not inherited:
        hard.append(f"설비 미지정 (후보: {', '.join(e for e in EQUIPMENT if e.startswith(eq['types'][0]))})")

    # 번호를 못 알아들었으면 직전 교신에서 같은 종류의 설비를 '추정'으로 붙인다 (확정하지 않음)
    guess = []
    if unclear and not eq["ids"]:
        prefix = eq["types"][0] if eq["types"] else None
        for h in reversed(history):
            ids = [e for e in h["equipment_ids"] if not prefix or e.startswith(prefix)]
            if ids:
                guess = ids
                hard.append(f"직전 교신 기준 {', '.join(EQUIPMENT[e] for e in ids)}로 추정")
                break
        text = annotate_guess(text, guess, "설비 번호가 들리지 않음" in hard)

    # "이번 컨베이어"는 2번 컨베이어로 본다 (팀 결정, radio_script.md #54도 CV-02 · 🟢 복창 대기로 맞춤)
    if "설비 미지정" in " ".join(hard) and not guess:
        need_llm.append("설비 번호가 없음")

    # 분류는 됐는데 빈칸이 남은 경우 → LLM에게 물어본다
    no_equipment = not (eq["ids"] or eq["unknown"] or eq["types"])
    if cls in ("가동지시", "정지지시", "정비요청", "복창") and no_equipment and "설비 번호가 들리지 않음" not in hard:
        need_llm.append("설비를 찾지 못함")
    if cls in ("가동지시", "정지지시") and action == "없음":
        need_llm.append("행위를 찾지 못함")
    if cls == "복창" and action == "없음" and not tasks:
        need_llm.append("복창 내용을 찾지 못함")

    return {
        "corrected_text": re.sub(r"^\s*\[호출\]\s*", "", text),  # 호출 이름 자리는 문장에서 뺀다 (DB는 call_text로 따로 저장)
        "equipment_ids": eq["ids"],
        "equipment_mentions": list(dict.fromkeys(eq["unknown"])),
        "equipment_guess": guess,  # 번호를 못 들었을 때 직전 교신 기준 추정 (확정 아님)
        "action": action if cls not in NO_ACTION_CLS else "없음",  # 상태보고·기타에는 운전 행위를 붙이지 않음
        "tasks": parts + tasks,
        "addressee": addressee(text, radio, cls),
        "called_by_name": text.lstrip().startswith("[호출]"),
        "inherited": inherited,
        "is_question": is_question(text),
        "eq_type": (eq["ids"] or guess)[0][:2] if (eq["ids"] or guess) else (eq["types"][0] if eq["types"] else None),
        "cls": cls,
        "need_llm": need_llm,
        "hard": hard,
    }


# ── LLM (Qwen) ────────────────────────────────────────

SYSTEM_PROMPT = f"""당신은 제철소 원료 설비 현장 무전에서 설비와 행위를 뽑습니다. JSON만 출력하세요.
발화 종류(cls)는 이미 정해져 있으니 바꾸지 마세요.

등록 설비: {", ".join(f"{k}={v}" for k, v in EQUIPMENT.items())}
무전기: A=운전실(지시자), B·C=현장 작업자

출력 JSON 키:
- equipment_ids: 등록 설비 ID 목록
- action: {" | ".join(ACTION_VALUES)}
- tasks: 정비 작업 목록 (예: "롤러 점검")
- inherited: 직전 지시의 설비·행위를 이어받았으면 true
- uncertain: 확신이 없으면 true
- reason: 한 줄 이유

규칙:
1. 설비 번호를 추측하지 마세요. 번호가 안 들렸으면 equipment_ids는 빈 목록, uncertain=true.
2. 등록 설비에 없는 번호는 equipment_ids에 넣지 말고 uncertain=true.
3. "아니, 3번", "5번."처럼 짧은 정정·답은 이전 교신의 지시를 이어받습니다.
4. 시운전·돌려·켜·살려는 가동, 세워·꺼·멈춰는 정지. "가동 금지"·"가동 취소"는 가동이 아닙니다.
5. 판정(LOTO 충돌 등)은 하지 마세요.

예시)
무전(A, 정지지시): 오번 벨트 좀 세워 봐
{{"equipment_ids": ["CV-05"], "action": "정지", "tasks": [], "inherited": false, "uncertain": false, "reason": "5번 벨트=5번 컨베이어, 세워=정지"}}
이전: [A] 2호기 파쇄기 가동해 (가동지시, CR-02) / 무전(A, 가동지시): 아니 그거 말고 일호기
{{"equipment_ids": ["CR-01"], "action": "가동", "tasks": [], "inherited": true, "uncertain": false, "reason": "정정, 직전 가동지시 이어받음"}}
"""


class QwenHelper:
    def __init__(self, model_id=DEFAULT_MODEL):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        from stt import pick_device
        self.torch = torch
        self.device, dtype = pick_device()
        print(f"LLM 로드: {model_id} ({self.device})")
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(model_id, dtype=dtype).to(self.device).eval()

    def ask(self, user, max_new_tokens=256):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        prompt = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                              enable_thinking=False)  # Qwen3로 바꿀 때 생각 모드 끔
        inputs = self.tok(prompt, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                      pad_token_id=self.tok.eos_token_id)
        return self.tok.decode(out[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def build_user_message(radio, raw, cls, rule, history, lex):
    lines = []
    if history:
        lines.append("이전 교신:")
        for h in history[-HISTORY_SIZE:]:
            lines.append(f"[{h['radio']}] {h['corrected_text']} ({h['cls']}, "
                         f"{','.join(h['equipment_ids']) or '-'}, {h['action']})")
    hints = lex.hints(raw)
    if hints:
        lines.append("관련 현장 용어: " + " / ".join(hints))
    lines.append(f"규칙 해석: {rule['corrected_text']} → 설비 {','.join(rule['equipment_ids']) or '-'}, "
                 f"행위 {rule['action']} (빈칸 이유: {', '.join(rule['need_llm']) or '없음'})")
    lines.append(f"무전({radio}, {cls}): {raw}")
    return "\n".join(lines)


def parse_llm_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("action") not in ACTION_VALUES:
        d["action"] = "없음"
    d["equipment_ids"] = [e.split(":")[-1].strip().upper() for e in (d.get("equipment_ids") or []) if isinstance(e, str)]
    d["tasks"] = [t for t in (d.get("tasks") or []) if isinstance(t, str)]
    d["inherited"] = bool(d.get("inherited"))
    d["uncertain"] = bool(d.get("uncertain"))
    d["reason"] = str(d.get("reason", ""))
    return d


def number_heard(eid, raw, rule_text):
    """LLM이 고른 설비 번호가 실제로 들린 말에 있었는가 (번호 지어내기 방지)."""
    n = int(eid[-2:])
    return str(n) in re.findall(r"\d+", rule_text) or KOR_DIGIT.get(n, "#") in raw


def llm_action(action, rule, raw):
    """LLM이 준 행위를 받을지: 운전 행위가 필요한 분류(가동·정지 지시, 작업 없는 복창)에서만 받는다.
    정비요청·상태보고·기타에 LLM이 붙인 행위('리셋' 등)는 버린다. '리셋'은 문장에 리셋이 있을 때만."""
    cls = rule.get("cls")
    if action == "없음" or cls in NO_ACTION_CLS or cls == "정비요청":
        return "없음"
    if cls == "복창" and rule["tasks"]:
        return "없음"  # 정비 작업 복창 ("라이너 점검 알겠습니다")
    if action == "리셋" and "리셋" not in raw:
        return "없음"
    return action


def merge(rule, llm, raw, history):
    out = dict(rule)
    reasons = list(rule["hard"])
    if llm is None:
        out["uncertain_reason"] = " / ".join(reasons + rule["need_llm"] + ["LLM 응답을 해석하지 못함"])
        out["uncertain"] = True
        out["source"] = "규칙(LLM 실패)"
        out["llm_check"] = "LLM 응답 오류"
        return out

    prev = last_order(history)
    inherited_ids = set(prev["equipment_ids"]) if llm["inherited"] and prev else set()
    ids = []
    for e in llm["equipment_ids"]:
        if e not in EQUIPMENT:
            reasons.append(f"LLM이 등록 안 된 설비 {e}를 제시")
        elif e in rule["equipment_ids"] or e in inherited_ids or number_heard(e, raw, rule["corrected_text"]):
            # 규칙도 같은 설비를 찾았으면(이어받기 포함) 번호가 문장에 없어도 인정
            ids.append(e)
        else:
            reasons.append(f"LLM이 들리지 않은 번호 {e}를 제시")
    # 교차 확인: 규칙이 이미 확신한 문장이면 LLM은 검산 역할. 설비나 행위가 서로 다를 때만 확인 필요로 올린다.
    rule_sure = not rule["need_llm"]
    diffs = []
    if rule["equipment_ids"] and ids and set(rule["equipment_ids"]) != set(ids):
        diffs.append(f"설비 규칙 {','.join(rule['equipment_ids'])} ↔ LLM {','.join(ids)}")
    fr, fl = family(rule["action"]), family(llm["action"])
    if fr and fl and fr != fl:
        diffs.append(f"행위 규칙 {rule['action']} ↔ LLM {llm['action']}")
    if diffs:
        reasons.append("규칙과 LLM 해석이 다름: " + " / ".join(diffs))
    elif llm["uncertain"] and not rule_sure:
        reasons.append(f"LLM: {llm['reason']}")
    check = ("불일치: " + " / ".join(diffs)) if diffs else "일치"

    out.update({
        "llm_check": check,
        "equipment_ids": rule["equipment_ids"] or ids,
        "action": rule["action"] if rule["action"] != "없음" else llm_action(llm["action"], rule, raw),
        "tasks": rule["tasks"] or llm["tasks"],  # LLM 작업어는 규칙이 하나도 못 찾았을 때만 (LLM이 "아이돌 교체"처럼 지어냄)
        "inherited": rule["inherited"] or llm["inherited"],
        "uncertain": bool(reasons),
        "uncertain_reason": " / ".join(reasons),
        "source": "규칙+LLM",
    })
    if out["equipment_ids"] and not out["eq_type"]:
        out["eq_type"] = out["equipment_ids"][0][:2]
    return out


LLM_MODES = {"all": "모든 문장 (규칙 + LLM 교차 확인)", "need": "규칙이 모를 때만", "off": "끄기 (규칙만)"}


class Extractor:
    """mode: all = 모든 문장을 LLM에도 보내 규칙과 교차 확인 (기본)
             need = 규칙이 빈칸을 못 채운 문장만 LLM에 보냄 (빠름)
             off = 규칙만"""

    def __init__(self, use_llm=True, model=DEFAULT_MODEL, mode="all"):
        self.lex = Lexicon()
        self.mode = mode if use_llm else "off"
        self.llm = QwenHelper(model) if self.mode != "off" else None
        self.llm_calls = 0

    def extract(self, radio, raw, cls, history=()):
        history = list(history)
        rule = rule_extract(radio, raw, cls, history, self.lex)
        if self.llm and (self.mode == "all" or rule["need_llm"]):
            self.llm_calls += 1
            answer = self.llm.ask(build_user_message(radio, raw, cls, rule, history, self.lex))
            out = merge(rule, parse_llm_json(answer), raw, history)
            out["llm_raw"] = answer.strip()
            return out
        out = dict(rule)
        reasons = rule["hard"] + rule["need_llm"]  # LLM을 안 썼으면 빈칸 사유도 확인 필요로 남김
        out.update({"uncertain": bool(reasons), "uncertain_reason": " / ".join(reasons),
                    "source": "규칙", "llm_raw": "", "llm_check": ""})
        return out


# ── 단독 실행 ─────────────────────────────────────────

def load_gold_equipment():
    gold = {}
    for line in SCRIPT_MD.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\|\s*\d+\s*\|\s*S\d+_\d+_[A-Z]\.wav", line):
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            equip = cols[5]
            gold[cols[1]] = None if ("후보" in equip or "없음" in equip) else sorted(set(re.findall(r"[A-Z]{2}-\d{2}", equip)))
    return gold


def main():
    from team_cls import cls_text, load_classifier

    p = argparse.ArgumentParser(description="설비·행위 추출 (팀 CLS + 규칙 + Qwen)")
    p.add_argument("--input", default=str(BASE / "stt_results.csv"))  # stt.py 결과 (stt_test.py보다 번호를 잘 들음)
    p.add_argument("--text-col", default="hyp")
    p.add_argument("--output", default=str(BASE / "llm_results.csv"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--llm", choices=list(LLM_MODES), default="all",
                   help="all: 모든 문장 교차 확인(기본) / need: 규칙이 모를 때만 / off: 규칙만")
    p.add_argument("--rules-only", action="store_true", help="--llm off 와 같음")
    args = p.parse_args()

    clf = load_classifier()
    ext = Extractor(model=args.model, mode="off" if args.rules_only else args.llm)
    gold = load_gold_equipment()
    rows = sorted((r for r in read_csv(args.input) if not r.get("error")), key=lambda r: r["file"])
    out, history, cls_hist, scene, start = [], [], [], None, 0
    ok = n = 0
    for r in rows:
        file, raw = r["file"], r[args.text_col]
        radio = re.search(r"_([A-Z])\.wav$", file).group(1)
        if file[:3] != scene:
            scene, history, cls_hist, start = file[:3], [], [], len(out)
        cls = clf.predict(cls_text(clean_text(raw)), cls_hist)["cls"]
        cls_hist.append(cls)
        res = ext.extract(radio, raw, cls, history)
        k = backfill_from_readback(history, radio, cls, res)
        if k is not None:  # 복창으로 원지시 설비를 추정했으면 그 줄도 고친다
            h = history[k]
            out[start + k].update(corrected_text=h["corrected_text"], equipment_guess=",".join(h["equipment_guess"]),
                                  eq_type=h["eq_type"], uncertain_reason=h["uncertain_reason"])
            print(f"   ↳ {out[start + k]['file']} 설비를 복창 기준으로 추정: {h['corrected_text']}")
        k = backfill_addressee(history, radio, cls)
        if k is not None:
            out[start + k]["addressee"] = history[k]["addressee"]
        history.append({**res, "cls": cls, "radio": radio})
        g = gold.get(file)
        if g is not None:
            n += 1
            ok += sorted(res["equipment_ids"]) == g
        print(f"{'🟡' if res['uncertain'] else '  '} {file:<14} {cls:<5} {','.join(res['equipment_ids']) or '-':<12} "
              f"{res['action']:<4} | {res['corrected_text']}")
        out.append({"file": file, "radio": radio, "stt_text": raw, "cls": cls,
                    **{k: (",".join(v) if isinstance(v, list) else v) for k, v in res.items()
                       if k not in ("need_llm", "hard")}})
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    checks = [r.get("llm_check", "") for r in out if r.get("llm_check")]
    agree = sum(c == "일치" for c in checks)
    print(f"\n저장: {args.output}\n설비 정확도: {ok}/{n} ({ok / max(n, 1):.1%})  LLM 호출: {ext.llm_calls}건"
          + (f"  규칙·LLM 일치: {agree}/{len(checks)}" if checks else ""))


if __name__ == "__main__":
    main()
