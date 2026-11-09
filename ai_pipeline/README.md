# ai_pipeline — 무전 STT · 교정 · 추출 · 판정 · 실시간 시연

무전 녹음을 받아 쓰고(STT), 현장 용어로 교정한 뒤, 팀 CLS로 분류하고, 설비·행위를 뽑아
팀 DB의 설비 상태(LOTO·작업허가)와 대조해 판정합니다. 교신이 끝나면 위험 상황 보고서 초안(웹·엑셀)을 만듭니다.

```
무전 녹음 → STT(Whisper large-v3, FP16) → 교정 → 분류(팀 CLS) → 설비·행위 추출(규칙 + LLM 교차 확인)
        → DB 저장(팀 db_api) → 판정(🔴충돌 🟠불일치 🟡확인 필요 🟢정상 ⚪기록) → 보고서 초안
```

이 폴더는 팀 저장소의 `db/`(DB 함수·표), `cls/`(분류기), `recordings/`(녹음), `risk/`(위험성평가 양식)를 그대로 가져다 씁니다.

## 실행 (저장소 맨 위에서)

```bash
cd ai_pipeline
pip install -r requirements.txt

# 1) 실시간 시연: ① 현장 무전 → ② DB 실시간 적재 → ③ 보고서 초안 (브라우저가 열림)
python live_demo.py --fresh                 # http://localhost:8765, 장면 S01 → S06 → S12
python live_demo.py --fresh --share         # 같은 와이파이의 다른 기기에서도 접속 (주소가 화면에 나옴)
python live_demo.py --scenes S01,S04        # 장면 고르기

# 2) 판정만 한 번에 (미리 돌린 LLM 결과 사용, 빠름)
python pipeline.py --from-llm-results

# 3) 시연 영상용 화면 (Streamlit)
streamlit run app.py
```

## 파일

| 파일 | 하는 일 |
|---|---|
| `stt.py` | Whisper STT (`openai/whisper-large-v3`, 현장 용어 프롬프트) + 표기 후처리 |
| `llm_post.py` | 교정(오인식 보정) + 설비·행위·받는 사람 추출. 규칙 + 로컬 LLM(Qwen2.5-3B) 교차 확인 |
| `team_cls.py` | 팀 CLS 분류기 불러오기 |
| `db.py` | 팀 `db/db_api.py` 연결부 (표 구조·버튼·장면은 팀 파일 사용) |
| `judge.py` | 판정 규칙 (충돌은 `loto_applied`·`permit_open` 기준, 복창은 원지시와 설비·행위 비교) |
| `pipeline.py` | 교정 → 분류 → 추출 → DB 저장 → 판정을 한 건씩 |
| `live_demo.py` | 실시간 시연 서버 + 보고서 초안(HTML·엑셀) 생성. 위험성평가 엑셀은 팀 `risk/src/make_draft.py` 사용 |
| `anim.py`, `app.py` | 시연 영상용 화면 (Streamlit) |
| `speed_test.py` | 실시간 속도 측정 (무전 1건씩, STT·후처리·판정까지 지연, RTF) |
| `ner_table.py`, `record.py`, `stt_test.py` | NER 표 만들기, 대본 녹음 도구, STT 간단 실행 |
| `ner_dictionary.csv`, `cls_dictionary.csv` | NER·분류 사전 |
| `stt_results.csv`, `whisper_results.csv`, `llm_results.csv` | STT 결과, LLM 추출 결과 (시연은 `llm_results.csv` 사용) |
| `radio_script_public.md` | 공개용 대본 (예상 판정은 뺌) |

## 결과 (대본 101건, `llm_results.csv` 기준)

- 판정 100/101, 🔴 충돌 19/19, 🟠 불일치 5/5, 오경보 0건, CLS 101/101
- STT 후처리 이후 단계(교정·분류·추출·판정·DB 저장)는 무전 1건당 1ms 미만 → 실시간 병목은 STT

## 실행하면 생기는 파일 (커밋하지 않음)

`live.db`(시연 DB, HeidiSQL로 열어 볼 수 있음), `radio.db`, `reports/`(보고서), `pipeline_results.csv`, `speed_results.csv`

원본 대본 `radio_script.md`(예상 판정 포함)는 올리지 않았습니다. 없으면 `pipeline.py`·`llm_post.py`의 채점만 건너뜁니다.
