"""팀 CLS 분류기(Hackerthon/cls) 불러오기.

팀 저장소 위치가 다르면 환경변수로 바꾼다:
    export HACKATHON_REPO=/다른/경로/Hackerthon
"""
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 팀 저장소 위치: 환경변수 → (팀 저장소 안의 폴더로 들어가 있으면) 바로 위 폴더 → ~/Documents/GitHub/Hackerthon
TEAM_REPO = Path(os.environ.get("HACKATHON_REPO") or (
    _HERE.parent if (_HERE.parent / "db" / "db_api.py").exists() else Path.home() / "Documents" / "GitHub" / "Hackerthon"))
# 녹음 폴더: 이 폴더에 없으면 팀 저장소의 recordings/
REC_DIR = _HERE / "recordings" if (_HERE / "recordings").exists() else TEAM_REPO / "recordings"


def load_classifier():
    src = TEAM_REPO / "cls" / "src"
    if not src.exists():
        raise FileNotFoundError(f"팀 CLS 폴더가 없습니다: {src} (HACKATHON_REPO 환경변수로 위치 지정)")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from classifier import UtteranceClassifier
    return UtteranceClassifier()


def cls_text(corrected_text):
    """CLS에 넣을 문장: 이름 호출 표시("[호출]")는 분류 규칙을 헷갈리게 하므로 뺀다."""
    return re.sub(r"^\s*\[호출\]\s*", "", corrected_text)
