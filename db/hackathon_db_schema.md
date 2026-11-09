# AI 복창 확인 시스템 – DB 구조 (수정안 반영)

`radio_script.md`, `instruction_script.md`의 설비 상태와 라벨 정의를 기준으로 만들고,
팀원이 보낸 **ERD 수정안 5가지**를 반영한 버전입니다. ERD 그림은 `hackathon_erd.png`입니다.

## 수정 사항 (이전 초안 대비)

| # | 수정 | 이유 |
|---|---|---|
| ① | `judgment.ref_msg_id`를 점선이 아니라 **실선 외래키**로 (N : 0..1) | 비교한 원지시를 정식 관계로 연결 |
| ② | `message_equipment`: `equipment_id` **NULL 허용** + `mention_text` 추가, PK를 `link_id`로 변경 | DB에 없는 설비(7번·9번 컨베이어)도 저장 |
| ③ | `radio_message`에 `addressee_id`(받는 무전기), `action`(행위) 추가 | 복창 짝짓기, 정지↔가동 불일치 판정 |
| ④ | `radio_message → risk_draft`를 **1 : 0..1 → 1 : 0..N** | 작업이 2개면 초안도 2개 |
| ⑤ | `state_change_log.msg_id` 추가 (NULL 가능) | 버튼을 누른 근거가 된 무전을 기록 (감사용) |
| ⑥ | `radio_message`에 `call_text`(부른 이름), `cls_score`, `cls_rule` 추가 | 호출을 이름 그대로 저장, 분류기 판정 이유 표시 |
| ⑦ | `risk_template`·`risk_step`·`risk_draft`에 위험성평가 담당의 컬럼을 합침 | 표준 양식 칸과 출처, 초안의 설비·엑셀 경로 |

## 1. ERD

```mermaid
erDiagram
  EQUIPMENT ||--o{ EQUIPMENT_ALIAS : "별칭"
  EQUIPMENT ||--|| EQUIPMENT_STATE : "현재 상태"
  EQUIPMENT ||--o{ STATE_CHANGE_LOG : "변경 이력"
  EQUIPMENT |o--o{ MESSAGE_EQUIPMENT : "언급됨 (없는 설비는 NULL)"
  RADIO_MESSAGE ||--o{ MESSAGE_EQUIPMENT : "언급한 설비"
  RADIO_MESSAGE ||--o| JUDGMENT : "판정"
  JUDGMENT }o--o| RADIO_MESSAGE : "비교한 원지시"
  STATE_CHANGE_LOG }o--o| RADIO_MESSAGE : "근거 무전"
  RADIO_MESSAGE ||--o{ RISK_DRAFT : "정비요청 시 생성 (작업 수만큼)"
  RISK_TEMPLATE ||--o{ RISK_STEP : "단계"
  RISK_TEMPLATE ||--o{ RISK_DRAFT : "바탕"
  EQUIPMENT |o--o{ RISK_DRAFT : "대상 설비"

  EQUIPMENT {
    string equipment_id PK
    string name
    string type
  }
  EQUIPMENT_ALIAS {
    int alias_id PK
    string equipment_id FK
    string alias
  }
  EQUIPMENT_STATE {
    string equipment_id PK
    string status
    boolean loto_applied
    boolean permit_open
    datetime updated_at
  }
  STATE_CHANGE_LOG {
    int log_id PK
    string equipment_id FK
    int msg_id FK "근거 무전, NULL 가능"
    string action
    datetime changed_at
    string changed_by
  }
  RADIO_MESSAGE {
    int msg_id PK
    string scenario_id
    string file_name
    string radio_id "말한 무전기"
    string addressee_id "받는 무전기 (대답한 무전기로 채움)"
    string call_text "부른 이름 (예: 창우야)"
    string stt_text
    string cls
    float cls_score "분류기 확신도"
    string cls_rule "적용한 규칙"
    string action "가동/정지/점검 등"
    datetime created_at
  }
  MESSAGE_EQUIPMENT {
    int link_id PK
    int msg_id FK
    string equipment_id FK "NULL 가능 (없는 설비)"
    string mention_text "들은 이름"
  }
  JUDGMENT {
    int judgment_id PK
    int msg_id FK
    int ref_msg_id FK "비교한 원지시"
    string level
    string reason
  }
  RISK_TEMPLATE {
    int risk_id PK
    string work_name
    string equipment_type
    string part
    string task
    string gap_note
  }
  RISK_STEP {
    int step_id PK
    int risk_id FK
    int step_no
    string hazard
    string countermeasure
    string task_step
    string hazard_class
    string likelihood
    string severity
    string risk_grade
    string source_file
    string source_row
    string fit
  }
  RISK_DRAFT {
    int draft_id PK
    int msg_id FK
    int risk_id FK
    string status
    string equipment_id FK
    datetime created_at
    string excel_path
    string pdf_path
  }
```

## 2. 표별 역할

| 표 | 역할 |
|---|---|
| EQUIPMENT | 설비 목록 (CV-01 ~ CV-05, CR-01, CR-02, FN-02) |
| EQUIPMENT_ALIAS | 설비 별칭 ("1컨", "오번 컨베이어" 등). 설비 정규화에 쓰는 사전 |
| EQUIPMENT_STATE | 설비의 **현재 상태** (정비 중, LOTO, 작업허가). 판정할 때 조회 |
| STATE_CHANGE_LOG | **담당자 버튼 조작 이력** (누가, 언제, 어떤 설비에, 무엇을, 근거 무전) |
| RADIO_MESSAGE | 무전 한 건 (STT 결과, CLS 분류와 확신도·규칙, 말한·받는 무전기, 부른 이름, 행위) |
| MESSAGE_EQUIPMENT | 한 무전이 언급한 설비 연결. **DB에 없는 설비**는 `equipment_id`가 NULL이고 들은 이름이 `mention_text`에 남음 |
| JUDGMENT | 판정 결과와 비교한 **원지시**(`ref_msg_id`) |
| RISK_TEMPLATE / RISK_STEP | 작업별 위험성 평가 양식과 단계 (세부작업, 분류, 위험 요인, 안전 대책, 가능성·중대성·위험성, 원본 출처) |
| RISK_DRAFT | 정비요청에서 생성된 위험성 평가 초안 (한 무전에 작업이 여러 개면 초안도 여러 개). 설비, 만든 시각, 엑셀 파일 위치 포함 |

> **이름 안내:** 기존 "JSA(작업안전분석)"를 팀 결정에 따라 **"위험성 평가"** 로 부릅니다. 표 이름도 `risk_template`, `risk_step`, `risk_draft`, 컬럼 이름은 `risk_id`로 바꿨습니다. (이전 이름: `jsa_template`, `jsa_step`, `jsa_draft`, `jsa_id`)

## 3. 값 정의 (제안)

| 컬럼 | 값 |
|---|---|
| EQUIPMENT_STATE.status | `READY`(운전 가능), `MAINTENANCE`(정비 중) |
| STATE_CHANGE_LOG.action | `MAINT_START`, `MAINT_DONE`, `LOTO_ON`, `LOTO_OFF`, `PERMIT_OPEN`, `PERMIT_CLOSE` |
| RADIO_MESSAGE.cls | 정비요청, 가동지시, 정지지시, 복창, 상태보고, 기타, **불명**(분류기가 확신하지 못할 때). 분류 전이면 NULL. 팀 분류기(`cls/src/common.py`)의 라벨과 같음 |
| RADIO_MESSAGE.action | 가동, 리셋, 정지, 정지유지, 가동금지, 가동취소 (NER 사전 ACTION 정규값, 분류기의 `ACTION_TO_CLS`와 같음). 점검·청소 같은 정비 작업(TASK)을 어디에 저장할지는 미정 |
| JUDGMENT.level | `충돌`, `불일치`, `확인 필요`, `정상`, `기록` (ERD 수정안 표기를 따름) |
| RISK_DRAFT.status | `DRAFT`(초안), `APPROVED`(담당자 확정) |
| RADIO_MESSAGE.addressee_id | `A`/`B`/`C`. 지시 뒤에 처음 대답(복창·되묻기)한 무전기로 채움. 이름(창우야 등)은 `call_text`에 따로 둠 |
| RADIO_MESSAGE.cls_score / cls_rule | 분류기 결과. 규칙으로 정해진 문장은 점수가 NULL이고 규칙 이름(예: `P5 가동+지시 어미`)만 남음 |
| RISK_TEMPLATE.equipment_type | `conveyor`, `crusher`, `fan` (EQUIPMENT.type과 같은 값) |
| RISK_STEP.fit | `직접`(같은 작업의 표준 데이터), `유사`(비슷한 작업에서 가져온 것) |

## 4. SQLite 생성문

참조되는 표가 먼저 만들어지도록 순서를 정리했습니다.

```sql
CREATE TABLE equipment (
  equipment_id TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  type         TEXT NOT NULL            -- conveyor / crusher / fan
);

CREATE TABLE equipment_alias (
  alias_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
  alias        TEXT NOT NULL UNIQUE
);

CREATE TABLE equipment_state (
  equipment_id TEXT PRIMARY KEY REFERENCES equipment(equipment_id),
  status       TEXT NOT NULL CHECK (status IN ('READY','MAINTENANCE')),
  loto_applied INTEGER NOT NULL DEFAULT 0,   -- 0/1
  permit_open  INTEGER NOT NULL DEFAULT 0,   -- 0/1
  updated_at   TEXT
);

CREATE TABLE radio_message (
  msg_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  scenario_id  TEXT,
  file_name    TEXT,
  radio_id     TEXT,                       -- 말한 무전기 (A / B / C)
  addressee_id TEXT,                       -- 받는 무전기 (③ 추가). 지시 뒤에 복창한 무전기로 채움
  call_text    TEXT,                       -- 부른 이름 (예: 창우야). 호출이 없으면 NULL
  stt_text     TEXT,
  cls          TEXT CHECK (cls IN ('정비요청','가동지시','정지지시','복창','상태보고','기타','불명')),  -- NULL = 아직 분류 전 (cls/src 분류기 라벨)
  cls_score    REAL,                       -- 분류기 확신도 (규칙으로 정해지면 NULL)  [CLS 담당 추가]
  cls_rule     TEXT,                       -- 분류기가 적용한 규칙 (예: P5 가동+지시 어미)  [CLS 담당 추가]
  action       TEXT,                       -- 가동 / 리셋 / 정지 / 정지유지 / 가동금지 / 가동취소 (NER 사전 ACTION 값, ③ 추가)
  created_at   TEXT
);

CREATE TABLE message_equipment (
  link_id      INTEGER PRIMARY KEY AUTOINCREMENT,                -- ② PK 변경
  msg_id       INTEGER NOT NULL REFERENCES radio_message(msg_id),
  equipment_id TEXT REFERENCES equipment(equipment_id),          -- ② NULL 가능 (DB에 없는 설비)
  mention_text TEXT                                              -- ② 들은 이름 (예: 7번 컨베이어)
);

CREATE TABLE state_change_log (
  log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
  msg_id       INTEGER REFERENCES radio_message(msg_id),         -- ⑤ 근거 무전 (NULL 가능)
  action       TEXT NOT NULL,
  changed_at   TEXT NOT NULL,
  changed_by   TEXT
);

CREATE TABLE judgment (
  judgment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id      INTEGER NOT NULL REFERENCES radio_message(msg_id),
  ref_msg_id  INTEGER REFERENCES radio_message(msg_id),          -- ① 비교한 원지시 (실선 FK)
  level       TEXT NOT NULL CHECK (level IN ('충돌','불일치','확인 필요','정상','기록')),
  reason      TEXT
);

CREATE TABLE risk_template (
  risk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  work_name      TEXT NOT NULL,            -- 예: 롤러 점검, 낙광 청소
  equipment_type TEXT,                     -- conveyor / crusher / fan  [위험성평가 담당 추가]
  part           TEXT,                     -- NER PART 정규값 (예: 롤러)
  task           TEXT,                     -- NER TASK 정규값 (예: 점검)
  gap_note       TEXT                      -- 표준 데이터에 없는 부분
);

CREATE TABLE risk_step (
  step_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  risk_id        INTEGER NOT NULL REFERENCES risk_template(risk_id),
  step_no        INTEGER NOT NULL,
  hazard         TEXT,
  countermeasure TEXT,
  task_step      TEXT,                     -- 세부작업 (작업 준비 / 작업명 / 작업 마무리)  [위험성평가 담당 추가]
  hazard_class   TEXT,                     -- 분류 (6대 위험요인)
  likelihood     TEXT,                     -- 가능성 (원본에 없으면 NULL)
  severity       TEXT,                     -- 중대성 (원본에 없으면 NULL)
  risk_grade     TEXT,                     -- 위험성 (원본에 없으면 NULL)
  source_file    TEXT,                     -- 원본 위험성평가 파일
  source_row     TEXT,                     -- 원본 행 번호
  fit            TEXT                      -- 직접 / 유사
);

CREATE TABLE risk_draft (
  draft_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id       INTEGER NOT NULL REFERENCES radio_message(msg_id),   -- ④ 한 무전에 여러 건 가능
  risk_id      INTEGER REFERENCES risk_template(risk_id),
  status       TEXT NOT NULL DEFAULT 'DRAFT',
  equipment_id TEXT REFERENCES equipment(equipment_id),             -- [위험성평가 담당 추가] 초안의 설비
  created_at   TEXT,                                                -- 만든 시각
  excel_path   TEXT,                                                -- 생성한 엑셀 파일 위치 (저장소 기준 상대경로)
  pdf_path     TEXT                                                 -- 같은 내용을 PDF로 바꾼 파일 위치
);
```

## 5. 초기 데이터 (radio_script 기준)

```sql
INSERT INTO equipment VALUES
  ('CV-01','1번 컨베이어','conveyor'),
  ('CV-02','2번 컨베이어','conveyor'),
  ('CV-03','3번 컨베이어','conveyor'),
  ('CV-04','4번 컨베이어','conveyor'),
  ('CV-05','5번 컨베이어','conveyor'),
  ('CR-01','1호기 파쇄기','crusher'),
  ('CR-02','2호기 파쇄기','crusher'),
  ('FN-02','2번 송풍기','fan');

INSERT INTO equipment_state (equipment_id, status, loto_applied, permit_open) VALUES
  ('CV-01','READY',0,0),
  ('CV-02','READY',0,0),
  ('CV-03','READY',0,0),
  ('CV-04','MAINTENANCE',1,0),   -- 정비 중, LOTO 적용
  ('CV-05','READY',0,0),
  ('CR-01','READY',0,0),
  ('CR-02','READY',0,0),
  ('FN-02','MAINTENANCE',0,1);   -- 정비 작업허가 진행 중, LOTO 미적용

INSERT INTO equipment_alias (equipment_id, alias) VALUES
  ('CV-01','1번 컨베이어'),('CV-01','일번 컨베이어'),('CV-01','1컨'),('CV-01','1번 벨트'),
  ('CV-02','2번 컨베이어'),('CV-02','2컨'),('CV-02','2번 벨트'),('CV-02','두 번째 컨베이어'),
  ('CV-03','3번 컨베이어'),('CV-03','삼번 컨베이어'),('CV-03','3컨'),('CV-03','3번 벨트'),
  ('CV-04','4번 컨베이어'),('CV-04','사번 컨베이어'),('CV-04','4컨'),('CV-04','4번 라인 컨베이어'),
  ('CV-05','5번 컨베이어'),('CV-05','오번 컨베이어'),('CV-05','5컨'),
  ('CR-01','1호기 파쇄기'),('CR-01','1호 파쇄기'),('CR-01','1호기 크러셔'),
  ('CR-02','2호기 파쇄기'),('CR-02','2호 파쇄기'),('CR-02','2호기 크러셔'),
  ('FN-02','2번 송풍기'),('FN-02','2번 블로워');
```

## 6. 아직 정해지지 않은 것

1. **"이번 컨베이어"**: radio_script 별칭 표에는 CV-02의 별칭인데 S08 #54에서는 "설비 모호"로 처리됨. 위 초기 데이터에서는 **일단 뺐습니다.**
2. **CLS 라벨**: 분류기 기준으로 `불명`을 포함해 7개로 정해짐 (DB에 CHECK로 반영). 남은 것: instruction_script의 복합 라벨 `정비요청 + 가동지시`는 분류기에 없음.
3. **"2번 다시 켜"**처럼 번호만 말해 컨베이어(CV-02)와 송풍기(FN-02)가 모두 후보인 경우의 처리.
4. **`radio_message.action`의 값 목록**은 NER 사전 기준 6개로 정해짐. 남은 것: AI 출력 형식, 정비 작업(TASK)을 저장할 위치.
5. 신뢰도는 `cls_score`로 반영됨. 남은 것: 작업 내용(`work_detail`), 누락 항목(`missing`). 필요하면 이후 추가.
6. 상태 변경은 **담당자 버튼**으로만 하므로, `equipment_state`를 바꾸는 코드는 버튼 처리 한 곳에만 두고 `state_change_log`에 같이 기록.

## 7. 팀원이 추가한 컬럼과 위험성 평가 데이터 (이번 반영)

팀원이 `hackathon_db_schema2.md`에서 ALTER로 덧붙인 컬럼을 위 SQL(`schema.sql`)의 CREATE 안으로 합쳤습니다.
DB를 사람마다 따로 만들면 구조가 달라지므로, **이 문서와 `schema.sql`을 기준으로 맞추는 것**을 권합니다.

| 표 | 추가된 칸 | 추가한 사람, 이유 |
|---|---|---|
| radio_message | `cls_score`, `cls_rule` | CLS 담당. 분류기 확신도와 적용 규칙 (화면에 판정 이유 표시) |
| radio_message | `call_text` | DB 담당. 부른 이름(창우야 등)을 글자 그대로 저장 |
| risk_template | `equipment_type`, `part`, `task`, `gap_note` | 위험성평가 담당. NER의 부품·작업 값으로 양식을 찾기 위함 |
| risk_step | `task_step`, `hazard_class`, `likelihood`, `severity`, `risk_grade`, `source_file`, `source_row`, `fit` | 위험성평가 담당. 표준 양식 칸과 출처 |
| risk_draft | `equipment_id`, `created_at`, `excel_path` | 위험성평가 담당. 초안의 설비, 시각, 엑셀 위치 |
| risk_draft | `pdf_path` | DB 담당. 엑셀을 PDF로 바꾼 파일 위치 (`risk/output/` 안의 파일을 가리킴) |

**위험성 평가 정답지 데이터**는 `db/risk_data.sql`에 있습니다(양식 9개, 단계 48행). 한국가스공사 위험성평가 표준모델 원문이고 출처 파일·행이 붙어 있으며, 가능성·중대성·위험성은 원본에 값이 없어 NULL입니다. `python build_db.py --with-risk-data`로 넣습니다.

**받는 무전기(`addressee_id`)** 는 이름이 아니라 **대답한 사람**으로 채웁니다(`build_db.py --classify`). 호출부호가 있는 대본 지시 28건 모두 정답과 같았습니다.

> `hackathon_db_schema2.md`는 이 문서의 이전 버전에 7번 절만 덧붙인 것이라 내용이 겹칩니다. 합친 뒤에는 하나만 남기는 것이 좋습니다.
