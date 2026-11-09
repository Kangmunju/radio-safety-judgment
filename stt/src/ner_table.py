"""대본 · STT 결과 · NER 표시를 한 표로: file, script, stt, ner (4열).

    file    녹음 파일
    script  대본 원본 (recordings.csv)
    stt     Whisper 결과(whisper_results.csv)를 교정한 문장 (llm_post.clean_text)
    ner     stt 문장의 사전 단어 옆에 [라벨:정규값] 표시 (ner_dictionary.csv)
            예) B, 3번 컨베이어[설비:CV-03] 다시 가동[행위:가동]해줘.

사용법:
    python ner_table.py                   # → stt_ner.csv
    python ner_table.py --out 다른이름.csv
"""
import argparse
import csv
import re
from pathlib import Path

from llm_post import EQUIPMENT, NER_CSV, TYPE_NAME, TYPE_WORD, clean_text, compact, read_csv

BASE = Path(__file__).resolve().parent
LABEL_KO = {"EQUIP": "설비", "EQUIP_TYPE": "설비종류", "ACTION": "행위", "TASK": "작업",
            "PART": "부품", "STATE": "상태", "HAZARD": "위험"}


def term_patterns():
    """사전 표현 → 정규식 (공백 유무 무시). 긴 표현부터. 한 글자 표현(켜, 꺼, 런)은 어절 맨 앞만."""
    pats = []
    for r in read_csv(NER_CSV):
        if r["label"] not in LABEL_KO:
            continue
        key = compact(r["term"])
        body = r"\s*".join(map(re.escape, key))
        if len(key) <= 1:
            body = rf"(?:(?<=\s)|^){body}(?=$|[\s.,?!]|줘)"
        doubt = "문맥" in (r.get("note") or "") or "모호" in (r.get("note") or "")
        pats.append((len(key), re.compile(body), r["label"], r["norm"], doubt))
    pats.sort(key=lambda x: -x[0])
    return pats


def tag(label, norm, surface, doubt=False):
    """doubt: 사전에 '문맥 확인'·'모호'로 적힌 표현 (예: 사건 → 4컨) → 값 뒤에 '?'"""
    q = "?" if doubt else ""
    if label == "EQUIP_TYPE" or compact(norm) == compact(surface):
        return f"[{LABEL_KO[label]}{q}]"
    return f"[{LABEL_KO[label]}:{norm}{q}]"


def annotate(text, pats):
    """긴 표현부터 찾고, 이미 표시한 자리와 겹치면 건너뛴다. 사전에 없는 'N번 컨베이어'(없는 설비 포함)도 표시."""
    used = [False] * len(text)
    spans = []  # (끝 위치, 붙일 표시)

    def take(start, end, label):
        if any(used[start:end]):
            return
        for k in range(start, end):
            used[k] = True
        spans.append((end, label))

    def dict_pass(labels):
        for _, rx, label, norm, doubt in pats:
            if label in labels:
                for m in rx.finditer(text):
                    take(m.start(), m.end(), tag(label, norm, m.group(0), doubt))

    dict_pass({"EQUIP"})  # 1) 사전에 있는 설비 이름
    # 2) 사전에 없는 "N번 컨베이어" (없는 설비 포함) — 일반 단어 "컨베이어"보다 먼저
    for m in re.finditer(r"(\d+)\s*(번|호기|호)\s*(라인\s*)?(컨베이어|컨|벨트|파쇄기|크러셔|송풍기|블로워)", text):
        eid = f"{TYPE_WORD[m.group(4)]}-{int(m.group(1)):02d}"
        take(m.start(), m.end(), f"[설비:{eid}]" if eid in EQUIPMENT else f"[설비:없음 {m.group(1)}번 {TYPE_NAME[eid[:2]]}]")
    dict_pass(set(LABEL_KO) - {"EQUIP"})  # 3) 나머지 (설비종류·행위·작업·부품·상태·위험)

    out = text
    for end, label in sorted(spans, reverse=True):  # 뒤에서부터 넣어야 앞 위치가 안 밀린다
        out = out[:end] + label + out[end:]
    return out


def main():
    p = argparse.ArgumentParser(description="대본 · STT · NER 4열 표 만들기")
    p.add_argument("--stt", default=str(BASE / "whisper_results.csv"), help="STT 결과 (file, text 열)")
    p.add_argument("--out", default=str(BASE / "stt_ner.csv"))
    args = p.parse_args()

    script = {r["file"]: r["text"] for r in read_csv(BASE / "recordings.csv")}
    stt = {r["file"]: r["text"] for r in read_csv(args.stt) if not r.get("error")}
    pats = term_patterns()
    rows = []
    for f in sorted(script):
        text = re.sub(r"^\s*\[호출\]\s*", "", clean_text(stt.get(f, "")))  # 이름 호출 자리는 문장에서 뺌
        rows.append({"file": f, "script": script[f], "stt": text, "ner": annotate(text, pats)})
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=["file", "script", "stt", "ner"])
        w.writeheader()
        w.writerows(rows)
    print(f"저장: {args.out} ({len(rows)}건)")


if __name__ == "__main__":
    main()
