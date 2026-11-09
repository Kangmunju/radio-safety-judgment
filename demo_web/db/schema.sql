-- AI 복창 확인 시스템 DB 스키마 (SQLite)
-- hackathon_db_schema.md 의 '4. SQLite 생성문'과 같은 내용입니다.
-- 참조되는 표가 먼저 만들어지도록 순서를 맞춰 두었습니다.

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

-- [규칙] 가동 지시의 충돌 판정은 status 가 아니라 loto_applied·permit_open 으로 한다.
--        (대본: LOTO 또는 작업허가가 걸린 설비에 가동 지시 = 충돌)
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

-- [규칙] 버튼별로 바뀌는 칸은 state_actions.csv 에 있다. 작업허가 종료(PERMIT_CLOSE)는 permit_open=0 으로 바꾸고,
--        loto_applied 가 0 일 때만 status 도 READY 로 바꾼다 (LOTO 가 걸린 설비는 정비 중으로 남는다).
CREATE TABLE state_change_log (
  log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
  msg_id       INTEGER REFERENCES radio_message(msg_id),         -- ⑤ 근거 무전 (NULL 가능)
  action       TEXT NOT NULL CHECK (action IN ('MAINT_START','MAINT_DONE','LOTO_ON','LOTO_OFF','PERMIT_OPEN','PERMIT_CLOSE')),   -- 담당자 버튼 6개 (state_actions.csv)
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
