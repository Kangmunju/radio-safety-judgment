"""CLS 공통: 경로, 라벨, 텍스트 정규화, NER 사전 용어 로드."""
import csv
import json
import re
from pathlib import Path

CLS_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = CLS_DIR / "data"
MODEL_DIR = CLS_DIR / "models"
REPORT_DIR = CLS_DIR / "reports"
ROOT = CLS_DIR.parent
SCRIPT_MD = ROOT / "teamfile" / "script" / "radio_script.md"
NER_DICT = ROOT / "ner_dictionary.csv"                         # 저장소 기준 경로
CLS_DICT = ROOT / "dictionary" / "cls_dictionary.csv"

TRAIN_FILE = DATA_DIR / "cls_train.jsonl"
EVAL_SCRIPT_FILE = DATA_DIR / "eval_script.jsonl"
MODEL_FILE = MODEL_DIR / "cls_tfidf_lr.joblib"
CONFIG_FILE = MODEL_DIR / "config.json"

LABELS = ["정비요청", "가동지시", "정지지시", "복창", "상태보고", "기타"]
UNKNOWN = "불명"
ORDER_LABELS = {"정비요청", "가동지시", "정지지시"}   # R10에서 이어받는 '지시'

# NER ACTION 정규값 → CLS (확정사항: 리셋 = 가동지시, 금지·중지 동일 처리)
ACTION_TO_CLS = {"가동": "가동지시", "리셋": "가동지시",
                 "정지": "정지지시", "정지유지": "정지지시", "가동금지": "정지지시", "가동취소": "정지지시"}
# NER 사전에 아직 없는 표현 (확정사항 미정 #3: NER 담당에 추가 요청)
EXTRA_TERMS = [("가동 중지", "ACTION", "가동금지"), ("중지", "ACTION", "정지")]


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def normalize(text):
    """STT·대본 공통 정규화: 따옴표·마침표류 제거, 공백 정리. 물음표는 질문 신호라 남긴다."""
    t = (text or "").strip().strip('"“”')
    t = re.sub(r"[.,!~…·]", " ", t)
    t = re.sub(r"\?", " ? ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_terms():
    """NER 사전의 ACTION·TASK 용어를 긴 것부터. '문맥 확인 필요' 표시된 용어는 오탐 위험이 있어 뺀다."""
    terms = list(EXTRA_TERMS)
    if NER_DICT.exists():
        with NER_DICT.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["label"] in ("ACTION", "TASK") and "문맥 확인" not in (r.get("note") or ""):
                    terms.append((r["term"], r["label"], r["norm"]))
    return sorted(set(terms), key=lambda x: -len(x[0]))
