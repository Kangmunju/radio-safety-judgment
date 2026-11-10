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
| ① STT | 무전 음성을 글자로 바꾸고(Whisper large-v3 + 현장 용어 프롬프트) 표기·오인식을 보정 | `stt/src/stt.py` |
| ② CLS | 발화를 정비요청·가동지시·정지지시·복창·상태보고·기타로 분류 (규칙 → 모델 → 불명) | `cls/` |
| ③ NER | 설비·행위·작업·상태·위험 용어를 사전으로 찾고, 빈칸은 로컬 LLM(Qwen2.5-3B)이 보조한 뒤 코드로 검증 | `ner_dictionary.csv`, `stt/src/llm_post.py` |
| ④ 판정 | DB의 설비 상태와 지시·복창을 규칙으로 대조해 충돌·불일치·확인 필요·정상·기록을 정함 | `db/judge.py` |
| ⑤ 위험성 평가 | 정비요청 무전마다 작업별 양식으로 위험성 평가 초안(엑셀·PDF) 작성 | `risk/` |
| 시연 화면 | 녹음 → STT → 분류 → 초안까지 한 화면에서 (Streamlit) / 실시간 관제 시연 | `demo_web/`, `ai_pipeline/` |

대본 101건 기준으로 글자 오류율은 보정 전 13.4% → 보정 후 1.6%, 분류 101/101, 판정 101/101이 대본의 예상값과 일치합니다. 자세한 수치는 `docs/`의 최종 보고서에 있습니다.

### 판정 단계

| 판정 | 의미 |
|---|---|
| 🔴 충돌 | LOTO 또는 정비 작업허가가 걸린 설비에 가동 지시 |
| 🟠 불일치 | 복창이 원지시와 다름 (설비 번호 또는 행위) |
| 🟡 확인 필요 | 지시 대상이 모호함, 설비 없음(DB에 없는 설비 포함), 원지시 없는 복창, 분류 불명 |
| 🟢 정상 | 충돌 없음, 복창 일치. 정지 지시와 정비 요청은 LOTO 설비라도 정상 |
| ⚪ 기록 | 판정 대상 아님 (상태보고, 기타) |

- 복창이 원지시와 일치해도 그 지시가 충돌이면 충돌을 유지합니다.
- "아니, 가동."처럼 설비 없이 정정하면 직전 지시의 설비를 이어받고, 번호가 있으면 새 지시로 봅니다.
- 복창이 일정 시간 안에 오지 않는 경우의 시간 기준 판정은 범위에 넣지 않았습니다.

## 폴더 구조

```
Hackerthon/
├─ ner_dictionary.csv        NER 사전 (설비·행위·작업·부품·상태·위험, 대본·STT 오인식·현장 용어)
├─ radio_script_public.md    공개용 무전 대본 (장면 13개, 무전 101건. 예상 판정은 뺌)
├─ 요구사항_명세서.md         요구사항 명세서 (기능 58개, 구현 현황)
├─ recordings/               팀원이 녹음한 무전 wav 101건 (S01_01_A.wav = 장면 S01의 1번째, A가 말함)
├─ dictionary/
│  └─ cls_dictionary.csv     CLS 규칙 사전 (우선순위별 키워드·어미, 50개 규칙)
├─ stt/
│  ├─ src/stt.py             STT 실행 (Whisper large-v3, 용어 프롬프트, 표기 후처리, 글자 오류율 평가)
│  ├─ src/llm_post.py        교정 + 설비·행위·받는 사람 추출 (규칙 + 로컬 LLM 교차 확인)
│  ├─ src/ner_table.py, stt_test.py   대본·STT·NER 비교표, 디코딩 파라미터 실험
│  ├─ src/requirements.txt   STT·LLM 실행에 필요한 패키지 (torch, transformers 등)
│  ├─ whisper_results.csv    Whisper 원문
│  ├─ stt_results.csv        STT 결과 (원문 hyp, 보정본 hyp_post, 글자 오류율)
│  └─ llm_results.csv        무전별 추출 결과 101건 (분류, 보정 문장, 설비, 행위, 작업, 받는 무전기, 불확실 여부)
├─ cls/
│  ├─ src/                   발화 분류기 (규칙 → 모델 → 불명), run.py
│  └─ models/                학습된 모델 (cls_tfidf_lr.joblib, config.json)
├─ db/
│  ├─ schema.sql             표 정의
│  ├─ seed.sql               설비 8개, 상태, 별칭 초기 데이터
│  ├─ risk_data.sql          위험성 평가 정답지 (양식 9개·위험요인 57행)
│  ├─ build_db.py            DB 파일 생성 스크립트
│  ├─ db_api.py              DB 함수 모음 (상태 조회, 담당자 버튼, 무전·판정 저장)
│  ├─ judge.py               판정 규칙 (충돌·불일치·확인 필요·정상·기록)
│  ├─ test_db_api.py, test_judge.py   db_api 시험 32개, 판정 시험 (대본 예상 판정 101건과 비교)
│  ├─ expected_judgments.csv 대본의 예상 판정을 5단계로 옮긴 정답
│  ├─ state_actions.csv      담당자 버튼별로 바뀌는 상태 칸
│  ├─ state_mapping.csv      무전에서 뽑은 상태 값 → 띄울 버튼
│  ├─ scenes.csv             장면별 시작 상태와 버튼 누르는 시점
│  └─ hackathon_db_schema.md, hackathon_erd.png   DB 설명·ERD
├─ risk/
│  ├─ run.py                 위험성 평가 초안 실행 (이것만 실행하면 됨)
│  ├─ src/                   make_draft.py(본체), build_templates.py, db.py
│  ├─ data/                  KRAS 양식 엑셀, 작업 9개·위험요인 57행 CSV, 바탕이 된 원문 15줄
│  ├─ output/                초안 결과물 (엑셀·PDF)
│  └─ README.md              위험성 평가 모듈 설명
├─ demo_web/
│  ├─ app.py, run.bat        시연 화면 (Streamlit): 녹음·업로드 → STT → 분류 → 위험성 평가 엑셀
│  └─ cls/, db/, risk/, stt/, dictionary/   위 모듈의 복사본 (원본을 고치면 함께 맞춰야 함)
├─ ai_pipeline/
│  ├─ live_demo.py           실시간 관제 시연 서버 (무전 수신 → 판정 → 보고서 초안)
│  ├─ pipeline.py, judge.py, db.py, team_cls.py   한 건씩 교정 → 분류 → 추출 → DB 저장 → 판정
│  ├─ app.py, anim.py        시연 영상용 화면 (Streamlit)
│  ├─ speed_test.py          실시간 속도 측정 (RTF, 판정까지 지연)
│  └─ README.md              실행 방법과 결과
└─ docs/
   ├─ 해커톤_프로젝트_최종_보고서.docx / .pdf
   ├─ 해커톤_PPT.pdf
   └─ 회의록.md / .pdf
```

## 시작하기

Python 3.13 기준으로 작성했습니다.

```bash
pip install scikit-learn joblib openpyxl          # DB, 분류, 판정, 위험성 평가 초안
pip install -r stt/src/requirements.txt           # 음성 인식·LLM 보정까지 돌릴 때 (torch, transformers)
pip install streamlit                             # 시연 화면
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
python build_db.py --with-stt --with-llm-results     # + llm_results.csv 의 설비·행위·분류 (message_equipment, action 채움)
python build_db.py --with-stt --with-llm-results --llm-text   # + 문장도 llm_results 의 보정 문장으로 교체
python build_db.py --with-stt --with-risk-data --with-risk-drafts
python build_db.py --out ../build/hackathon.db       # 출력 위치 지정
```

`--with-llm-results`는 `--with-stt`와 함께 써야 하고, `--llm-text`는 `--with-llm-results`와 함께 써야 합니다.
- `action`은 llm_results 값을 넣고 ("없음"은 비움), `message_equipment`는 `equipment_ids`마다 한 행을 만듭니다. DB에 없는 설비는 들린 이름(`mention_text`)만 남깁니다.
- `cls`와 복창의 `addressee_id`는 비어 있을 때만 채웁니다. 지시의 받는 무전기는 `불명`이 많아서 쓰지 않습니다.
- llm_results에서 설비를 못 찾은 무전(예: "3"이 빠진 S01_01_A)은 문장에서 설비 별칭을 찾아 채웁니다.
- 기본은 문장을 `stt_results.csv`의 `hyp_post`로 두고, `--llm-text`를 주면 `corrected_text`로 바꿉니다.

### 2. DB 함수 쓰기 (`db/db_api.py`)

SQL을 몰라도 쓸 수 있게 자주 쓰는 동작을 함수로 묶었습니다. 먼저 DB를 만드세요 (기본 경로 `db/hackathon.db`, 환경변수 `HACKATHON_DB`로 바꿀 수 있음).

```bash
python db/build_db.py --with-stt --classify --with-risk-data --with-risk-drafts
```

```python
import sys; sys.path.insert(0, "db")
import db_api

db_api.get_state("CV-03")                           # 현재 상태 (start_blocked: 가동 지시가 오면 충돌이 되는 상태인지)
db_api.suggest_buttons(["정비완료", "LOTO해제"])      # 무전에서 뽑은 상태 값 → 띄울 담당자 버튼
db_api.press_button("CV-03", "LOTO_ON", msg_id=3)   # 담당자 버튼: 상태 변경 + 기록
db_api.find_equipment("삼번 컨베이어 다시 가동해")     # 문장 속 설비 별칭 → 설비 번호
db_api.reset_scene("S01")                           # 장면을 처음 상태로 되돌리기 (시연 전에 사용)
```

그 밖에 `get_all_states`(현황판), `scene_buttons_after`(이 무전 다음에 누르는 버튼), `save_message`, `save_judgment`, `list_messages`가 있습니다. 시험은 `python db/test_db_api.py`로 돌립니다 (임시 DB를 새로 만들어 대본의 상태 변화를 확인).

### 3. 발화 분류 돌려보기

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
무전 101건은 규칙 100건, 모델 1건, 불명 0건으로 분류됐습니다.

### 4. 판정 돌려보기 (`db/judge.py`)

DB에 무전과 분류가 들어 있어야 합니다 (`--with-stt --classify`). 장면마다 설비 상태를 처음으로 되돌리고, `db/scenes.csv`에 적힌 시점에 담당자 버튼을 누른 것으로 보고 한 건씩 판정합니다.

```bash
python db/judge.py                                   # 모든 장면 판정, judgment 표에 저장
python db/judge.py --scene S01                       # 장면 하나만
python db/judge.py --no-save                         # 저장하지 않고 화면에만 출력
python db/test_judge.py                              # 대본 예상 판정 101건과 비교
```

```python
import sys; sys.path.insert(0, "db")
import judge

judge.judge_scene("S01")                            # 장면 하나를 처음부터 끝까지 판정하고 저장
judge.judge_all()                                   # 모든 장면
```

판정 결과에는 비교한 원지시를 함께 저장해 위험 상황 이력으로 남깁니다.

### 5. 위험성 평가 초안 만들기 (`risk/run.py`)

DB의 정비요청 무전마다 KRAS 양식 엑셀을 한 장씩 만듭니다. 같은 설비의 LOTO 적용·정비 완료 무전은 작업 기록 줄에 들어갑니다.

```bash
python db/build_db.py --with-stt --classify --with-risk-data
python risk/run.py                                   # 정비요청 전부 → risk/output/<무전파일>_위험성평가.xlsx
python risk/run.py S01                               # 장면 하나만
```

가능성·중대성·위험성 칸은 비워 두며 담당자가 입력합니다. 자세한 내용은 `risk/README.md`를 보세요.

### 6. 음성 인식 돌려보기 (`stt/src/stt.py`)

GPU가 있으면 빠르고, 없으면 `--cpu`로 돌릴 수 있습니다. 처음 실행할 때 Whisper 모델을 내려받습니다.

```bash
python stt/src/stt.py                                # 101건 받아쓰기 + 글자 오류율 평가 → stt_results.csv
python stt/src/stt.py --scene S01                    # 장면 하나만
python stt/src/stt.py --file recordings/S01_04_A.wav # 파일 하나만
python stt/src/llm_post.py --rules-only              # 설비·행위 추출 (규칙만) / 옵션 없이 실행하면 LLM 교차 확인
```

### 7. 시연 화면 띄우기

```bash
demo_web\run.bat                                     # 또는 cd demo_web && streamlit run app.py
```

녹음하거나 wav를 올리면 STT → 분류 → 정비요청이면 위험성 평가 엑셀까지 한 화면에서 이어집니다. 인식 문장이 틀리면 화면에서 고칠 수 있습니다.
실시간 관제 시연(무전 수신 → 판정 → 경보 → 보고서 초안)은 `ai_pipeline/live_demo.py`로 띄웁니다 (`ai_pipeline/README.md`).

## 설비와 초기 상태

시연과 테스트는 아래 초기 상태에서 시작합니다 (`db/seed.sql`).

| 설비 ID | 이름 | 초기 상태 |
|---|---|---|
| CV-01, 02, 03, 05 | 1·2·3·5번 컨베이어 | 운전 가능 |
| CV-04 | 4번 컨베이어 | **정비 중, LOTO 적용** |
| CR-01, CR-02 | 1·2호기 파쇄기 | 운전 가능 |
| FN-02 | 2번 송풍기 | **정비 작업허가 진행 중, LOTO 미적용** |

- 설비 상태는 **담당자 버튼**(`press_button`)으로만 바꿉니다. 변경 내용은 `state_change_log`에 함께 기록합니다.
- **충돌 판정은 `status`가 아니라 `loto_applied`와 `permit_open`으로 합니다.** 둘 중 하나라도 걸려 있으면 가동 지시는 충돌입니다 (`get_state`의 `start_blocked`).
- 버튼별로 바뀌는 칸은 `db/state_actions.csv`에 있습니다.

| 버튼 | 바뀌는 칸 |
|---|---|
| 정비 시작 / 정비 완료 | `status` → MAINTENANCE / READY |
| LOTO 적용 / 해제 | `loto_applied` → 1 / 0 |
| 작업허가 시작 / 종료 | `permit_open` → 1 / 0. 종료할 때 `loto_applied`가 0이면 `status`도 READY |

- 무전에서 뽑은 상태 값을 어떤 버튼으로 이어 줄지는 `db/state_mapping.csv`에 있습니다. 예를 들어 "LOTO 걸었습니다"에는 [LOTO 적용]과 [정비 시작]을 함께 띄웁니다. "로터"처럼 LOTO 오인식이라 적용인지 해제인지 알 수 없는 값은 기록만 하고 버튼을 권하지 않습니다.
- `db/scenes.csv`는 장면별 시작 상태(S12는 CV-03이 정비 중+LOTO에서 시작)와 각 무전 뒤에 담당자가 누르는 버튼을 담습니다.
- "이번 컨베이어"처럼 설비를 특정할 수 없는 말은 별칭에 넣지 않고, 판정 단계에서 `확인 필요`로 처리합니다.

## DB 표

`equipment`, `equipment_alias`, `equipment_state`, `radio_message`, `message_equipment`, `state_change_log`, `judgment`, `risk_template`, `risk_step`, `risk_draft`

자세한 컬럼과 관계는 `db/hackathon_db_schema.md`, `db/hackathon_erd.png`를 보세요.

## 사전 파일

| 파일 | 내용 | 추가할 때 |
|---|---|---|
| `ner_dictionary.csv` | `term, label, norm, source, note`. label은 EQUIP, EQUIP_TYPE, ACTION, TASK, PART, STATE, HAZARD, RADIO, CUE | 새 설비 별칭이나 STT 오인식을 한 줄 추가 |
| `dictionary/cls_dictionary.csv` | `priority, cls, type, pattern, condition, example, note` | 단어만 추가하면 코드 수정 없이 반영됨 |

- `source`는 `대본`, `STT 오인식`, `현장 용어` 중 하나입니다.
- `note`에 "문맥 확인 필요"나 "모호"가 적힌 용어는 오탐 위험이 있어 분류기가 쓰지 않고, DB 별칭에도 기본으로 넣지 않습니다.

## 알려진 한계

- 분류·판정의 101/101은 팀이 만든 대본의 정답과 비교한 값입니다. 대본에 없는 표현과 실제 현장 무전에 대한 성능은 현업 검증이 필요합니다. 무전 지시·복창 방식도 일반적인 안전 교신 절차를 기준으로 만든 시나리오입니다.
- 판정 코드는 두 벌입니다. `db/judge.py`(장면 단위 일괄 판정, 101/101)와 `ai_pipeline/judge.py`(실시간 시연용, 100/101). 규칙을 고치면 둘 다 맞춰야 합니다.
- `demo_web/`은 분류·DB·초안·인식 코드의 복사본을 따로 갖고 있고, 시연 화면에는 판정 결과(충돌·불일치)가 나오지 않습니다. 판정까지 보여 주는 화면은 `ai_pipeline/`입니다.
- 원본 대본 `radio_script.md`(예상 판정 포함)와 분류기 학습 데이터(`cls/data/`)는 저장소에 올리지 않았습니다. 분류·판정만 돌릴 때는 필요하지 않고, 없으면 채점만 건너뜁니다.
- 위험성 평가의 가능성·중대성·위험성 칸은 비어 있습니다 (정답지 원본에 값이 없어 DB에서도 NULL, 엑셀 출력도 빈칸). AI가 정하지 않고 담당자가 입력합니다.
- 위험요인 57행은 한국가스공사 위험성평가 표준모델을 바탕으로 해서 제철소 고유의 위험(낙광 쏟아짐, 슈트 내부 진입 등)은 들어 있지 않습니다. 양식마다 "표준 데이터 없음" 메모로 표시했습니다.
- 설비 8대, 무전 대본, 녹음은 시연용 가상 데이터입니다. 실제 설비 정보가 아니며, 현장 소음과 다양한 화자에서는 검증하지 못했습니다.

