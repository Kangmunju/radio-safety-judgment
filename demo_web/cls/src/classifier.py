"""발화 분류기 (팀 파이프라인에서 import 해서 쓰는 모듈). 방식 A: 규칙 → 모델 → 불명.

사용법:
    from classifier import UtteranceClassifier
    clf = UtteranceClassifier()
    clf.predict("B, 3번 컨베이어 다시 가동해 줘.", history=["정비요청", "복창"])
    → {"cls": "가동지시", "score": None, "rule": "P5 가동+지시 어미", "top": "가동지시"}

판단 순서 (확정사항.md 3장)
1. 팀 규칙 사전 cls_dictionary.csv (rules.py) — 걸리면 그 라벨 (score = None)
2. 규칙에 안 걸리면 모델 예측
3. 모델 확신도가 기준값(config.json threshold)보다 낮으면 '불명'
history: 같은 대화에서 앞선 발화들의 cls 목록 (오래된 것 → 최근 순). '아니, 5번.' 같은 이어받기에 쓴다.
"""
import json

import joblib

from common import CONFIG_FILE, MODEL_FILE, UNKNOWN, normalize
from rules import RuleEngine


class UtteranceClassifier:
    def __init__(self, model_path=MODEL_FILE, config_path=CONFIG_FILE, threshold=None, use_rules=True):
        self.pipe = joblib.load(model_path)
        cfg = json.loads(open(config_path, encoding="utf-8").read())
        self.threshold = cfg["threshold"] if threshold is None else threshold
        self.rules = RuleEngine() if use_rules else None

    def predict(self, text, history=()):
        t = normalize(text)
        if self.rules:
            r = self.rules.apply(t, history)
            if r:
                return {"cls": r[0], "score": None, "rule": r[1], "top": r[0]}
        proba = self.pipe.predict_proba([t])[0]
        i = int(proba.argmax())
        top, score = str(self.pipe.classes_[i]), float(proba[i])
        if score < self.threshold:
            return {"cls": UNKNOWN, "score": score, "rule": f"모델 확신도 {score:.2f} < {self.threshold} → 불명", "top": top}
        return {"cls": top, "score": score, "rule": "모델", "top": top}
