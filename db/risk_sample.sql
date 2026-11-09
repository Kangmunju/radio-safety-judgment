-- 위험성 평가 양식 '샘플' 데이터 (예시용)
-- 실제 포스코 양식이 아니라 구조를 확인하기 위해 직접 지어낸 내용입니다.
-- 팀에서 정식 양식을 받으면 이 파일을 바꾸거나 대체하세요.
-- 사용: python build_db.py --with-risk-sample
--
-- 현재 스키마(schema.sql)의 risk_template은 work_name만 있고 설비 종류 칸이 없어서
-- 작업 이름 앞에 설비 종류를 붙여 두었습니다.

INSERT INTO risk_template(work_name) VALUES
  ('공통 단계'),
  ('컨베이어 롤러 점검'),
  ('컨베이어 낙광 청소');

INSERT INTO risk_step(risk_id, step_no, hazard, countermeasure) VALUES
  (1, 1, '작업 범위 미확인',      '작업 전 위험 확인, 인원 배치'),
  (1, 2, '설비 갑작스러운 가동',  '전원 차단, 잠금·표지 부착 (LOTO)'),
  (1, 3, '인원 잔류 상태 재가동', '인원 확인 후 LOTO 해제'),
  (2, 1, '회전부 끼임',           '정지 확인 후 접근'),
  (3, 1, '낙하물, 분진',          '보호구 착용, 하부 접근 통제');
