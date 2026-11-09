"""위험성평가 한 번에 실행 (저장소 맨 위 폴더에서).

    python risk/run.py                     # 정비요청 전부 → risk/output/<무전파일>_위험성평가.xlsx
    python risk/run.py S01                 # 장면 하나만
    python risk/run.py --records-in-body   # 무전 기록을 본문 칸에도 채운 _기록반영.xlsx 도 함께
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from make_draft import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:])
