"""risk/data 의 템플릿 CSV → 팀 db/risk_data.sql 다시 만들기.

팀 DB(python db/build_db.py --with-risk-data)는 db/risk_data.sql 을 읽어 risk_template·risk_step 을 채운다.
템플릿(build_templates.py)을 바꿨다면 이걸 실행해 risk_data.sql 도 같은 내용으로 맞춘다.
    python risk/src/db.py
"""
import csv
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "risk" / "data"
TABLES = (("risk_template", "risk_template.csv"), ("risk_step", "risk_step.csv"))


def read_rows(file):
    with (DATA / file).open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def export_risk_sql(path=REPO / "db" / "risk_data.sql"):
    def q(v):
        if v in (None, ""):
            return "NULL"
        return v if v.isdigit() else "'" + v.replace("'", "''") + "'"
    lines = ["-- 위험성 평가 정답지 데이터 (위험성평가 담당 작성)",
             "-- 대본의 정비 작업 9개(risk_template)와 작업별 위험요인·감소대책(risk_step).",
             "-- 사용자 입력(예시): 한국가스공사 위험성평가 표준모델 문장의 어투를 쓰고 설비·부품·작업 단어만 바꿨습니다.",
             "-- source_file·source_row = 바탕이 된 원문 행(risk/data/source_rows.csv), fit = '원문 그대로' / '단어 교체'.",
             "-- 가능성·중대성·위험성은 담당자가 입력하므로 NULL입니다.",
             "-- 원본: risk/data/*.csv (risk/src/build_templates.py 로 생성)  ·  다시 만들기: python risk/src/db.py",
             "-- 사용: python build_db.py --with-risk-data", "",
             "DELETE FROM risk_step;", "DELETE FROM risk_template;", ""]
    for table, file in TABLES:
        rows = read_rows(file)
        cols = list(rows[0].keys())
        lines.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES")
        lines += [f"  ({', '.join(q(r[c]) for c in cols)})" + ("," if i < len(rows) - 1 else ";")
                  for i, r in enumerate(rows)]
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {path}")


if __name__ == "__main__":
    export_risk_sql()
