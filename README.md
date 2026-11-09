# AI 복창 확인 시스템

제철소 AI 해커톤 · 팀 **레츠 포스꼬우**
대주제: AI로 더 안전하고, 멈추지 않고, 더 정확하게 생산하는 제철소

무전으로 오가는 작업 지시를 AI가 듣고 **기록**(위험성 평가 초안)과 **확인**(설비 상태·복창 대조)을 만들어 주는 시스템입니다.
현장의 말은 그대로 두고, 그 말에서 기록과 확인을 붙입니다.

> **AI는 이해하고, 판정은 규칙이 한다.**
> 위험을 낮추는 변경(정비 완료, LOTO 해제)은 담당자가 확인해야 반영된다.

## 처리 흐름

```
무전 음성 → ① STT → ② 발화 분류(CLS) → ③ 개체 추출(NER) → ④ 판정 ─┬→ 설비 상태·복창 대조 결과
                                                                      └→ ⑤ 위험성 평가 초안 → 담당자 확정
```

| 단계 | 하는 일 | 위치 |
|---|---|---|
| ① STT | 무전 음성을 글자로 바꾸고 오인식을 보정 | `stt/` |
| ② CLS | 발화를 정비요청·가동지시·정지지시·복창·상태보고·기타로 분류 | `cls/` |
| ③ NER | 설비·행위·작업·상태·위험 용어를 사전으로 찾음 | `ner_dictionary.csv` |
| ④ 판정 | DB의 설비 상태와 지시·복창을 규칙으로 대조 | `db/` |
| ⑤ 위험성 평가 | 작업 내용에서 위험성 평가 초안(엑셀·PDF) 작성 | `risk/` |

### 판정 단계

| 판정 | 의미 |
|---|---|
| 🔴 충돌 | LOTO 또는 정비 작업허가가 걸린 설비에 가동 지시 |
| 🟠 불일치 | 복창이 원지시와 다름 (설비 번호 또는 행위) |
| 🟡 확인 필요 | 지시 대상이 모호함, 설비 없음, 복창 없음 |
| 🟢 정상 | 충돌 없음, 복창 일치 |
| ⚪ 기록 | 판정 대상 아님 (상태보고, 기타) |

## 폴더 구조

```
Hackerthon/
├─ ner_dictionary.csv        NER 사전 (설비·행위·작업·부품·상태·위험, 대본·STT 오인식·현장 용어)
├─ dictionary/
│  └─ cls_dictionary.csv     CLS 규칙 사전 (우선순위별 키워드·어미)
├─ stt/
│  └─ stt_results.csv        STT 결과 (원문 hyp, 보정본 hyp_post, 글자 오류율)
├─ cls/
│  ├─ src/                   발화 분류기 (규칙 → 모델 → 불명)
│  └─ models/                학습된 모델 (cls_tfidf_lr.joblib, config.json)
├─ db/
│  ├─ schema.sql             표 정의
│  ├─ seed.sql               설비 8개, 상태, 별칭 초기 데이터
│  ├─ risk_data.sql          위험성 평가 정답지 (양식·단계)
│  ├─ build_db.py            DB 파일 생성 스크립트
│  ├─ hackathon_db_schema*.md, hackathon_erd.png   DB 설명·ERD
└─ risk/
   └─ output/                위험성 평가 초안 결과물 (엑셀·PDF)
```

## 시작하기

Python 3.13 기준으로 작성했습니다.

```bash
pip install scikit-learn joblib
```

### 1. DB 만들기

DB 파일(`*.db`)은 저장소에 올리지 않습니다. 필요할 때 `db/build_db.py`로 새로 만듭니다.
실행할 때마다 기존 DB를 지우고 다시 만듭니다.

```bash
cd db
python build_db.py                                   # 표 + 초기 데이터만
python build_db.py --ner-aliases script              # + NER 사전의 대본 별칭
python build_db.py --with-risk-data                  # + 위험성 평가 정답지
python build_db.py --with-stt --classify             # + STT 결과와 분류 결과
python build_db.py --with-stt --with-risk-data --with-risk-drafts
python build_db.py --out ../build/hackathon.db       # 출력 위치 지정
```

### 2. 발화 분류 돌려보기

저장소 맨 위 폴더에서 실행합니다.

```bash
python cls/src/run.py                                # STT 보정본(hyp_post) 분류
python cls/src/run.py --col hyp                      # STT 원본 분류
python cls/src/run.py --file 다른파일.csv --col 열이름
```

코드에서 쓸 때:

```python
from classifier import UtteranceClassifier

clf = UtteranceClassifier()
clf.predict("B, 3번 컨베이어 다시 가동해 줘.", history=["정비요청", "복창"])
# → {"cls": "가동지시", "score": None, "rule": "...", "top": "가동지시"}
```

판단 순서: **규칙 사전(`cls_dictionary.csv`) → 모델 → 불명.**
규칙에 걸리면 그 라벨을 쓰고, 안 걸리면 모델이 예측합니다. 모델 확신도가 기준값(0.35)보다 낮으면 `불명`입니다.
`history`는 앞선 발화들의 분류 목록이며, "아니, 5번."처럼 직전 지시를 이어받는 정정 발화에 씁니다.

모델: TF-IDF(글자 1~4그램) + 로지스틱 회귀. 학습 데이터 250건, 교차검증 정확도 0.88 (`cls/models/config.json`).

## 설비와 초기 상태

시연과 테스트는 아래 초기 상태에서 시작합니다 (`db/seed.sql`).

| 설비 ID | 이름 | 초기 상태 |
|---|---|---|
| CV-01, 02, 03, 05 | 1·2·3·5번 컨베이어 | 운전 가능 |
| CV-04 | 4번 컨베이어 | **정비 중, LOTO 적용** |
| CR-01, CR-02 | 1·2호기 파쇄기 | 운전 가능 |
| FN-02 | 2번 송풍기 | **정비 작업허가 진행 중, LOTO 미적용** |

- 설비 상태는 **담당자 버튼**으로만 바꿉니다. `equipment_state`를 바꾸는 코드는 버튼 처리 한 곳에만 두고, 변경 내용은 `state_change_log`에 기록합니다.
- "이번 컨베이어"처럼 설비를 특정할 수 없는 말은 별칭에 넣지 않고, 판정 단계에서 `확인 필요`로 처리합니다.

## DB 표

`equipment`, `equipment_alias`, `equipment_state`, `radio_message`, `message_equipment`, `state_change_log`, `judgment`, `risk_template`, `risk_step`, `risk_draft`

자세한 컬럼과 관계는 `db/hackathon_db_schema.md`, `db/hackathon_db_schema2.md`, `db/hackathon_erd.png`를 보세요.

## 사전 파일

| 파일 | 내용 | 추가할 때 |
|---|---|---|
| `ner_dictionary.csv` | `term, label, norm, source, note`. label은 EQUIP, EQUIP_TYPE, ACTION, TASK, PART, STATE, HAZARD, RADIO, CUE | 새 설비 별칭이나 STT 오인식을 한 줄 추가 |
| `dictionary/cls_dictionary.csv` | `priority, cls, type, pattern, condition, example, note` | 단어만 추가하면 코드 수정 없이 반영됨 |

- `source`는 `대본`, `STT 오인식`, `현장 용어` 중 하나입니다.
- `note`에 "문맥 확인 필요"나 "모호"가 적힌 용어는 오탐 위험이 있어 분류기가 쓰지 않고, DB 별칭에도 기본으로 넣지 않습니다.

## 알려진 한계

- 이 저장소에는 STT 실행 코드, 판정 엔진, 위험성 평가 생성 코드가 아직 없습니다. 지금은 STT 결과 파일, 분류기, DB, 위험성 평가 결과물만 있습니다.
- 분류기의 `common.py`는 `cls/data/`와 `teamfile/script/radio_script.md`를 참조합니다. 이 경로는 학습·평가용이며 저장소에 포함되어 있지 않습니다. 분류만 돌릴 때는 필요하지 않습니다.
- 위험성 평가의 가능성·중대성·위험성 값은 AI가 정하지 않고 사람이 입력합니다.
- 설비 상태와 템플릿은 시연용 가상 데이터입니다. 실제 설비 정보가 아닙니다.

