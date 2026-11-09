"""무전 안전 모니터링 시스템 (시연 화면).

실행:
    source ~/Documents/GitHub/study/jisu/bin/activate
    streamlit run app.py

운전실처럼 한 화면에서 보여 준다:
  현장 상황(운전실·작업자 무전) · AI 판정과 경보 · 실시간 무전 기록 · 설비 상태 · DB 적재
처음 열 때 모든 장면을 미리 분석해 두고, [▶ 무전 수신 시작]을 누르면 재생한다.
장면·입력 문장은 왼쪽 사이드바(>)에서 바꾼다.
"""
import re
import wave
from pathlib import Path

import streamlit as st

import anim
import db
from llm_post import EQUIPMENT, LLM_MODES, Extractor, read_csv
from team_cls import load_classifier

BASE = Path(__file__).resolve().parent
SCRIPT_MD = BASE / "radio_script.md"
SCRIPT_CSV = BASE / "recordings.csv"      # 대본 문장
STT_CSV = BASE / "stt_results.csv"        # Whisper 결과 (stt.py)
from team_cls import REC_DIR  # noqa: E402  녹음 폴더 (없으면 팀 저장소 recordings/)

PUBLIC_MD = BASE / "radio_script_public.md"  # 장면 제목 (답이 들어 있지 않은 공개용)

STORY = anim.STORY  # 장면별 상황 소개 (anim.py)
DEMO_DEFAULT = ["S01", "S06", "S12"]  # 연속 재생(영상용) 기본 장면: 충돌 · 복창 불일치 · 청소 목적 가동

st.set_page_config(page_title="무전 안전 모니터링", page_icon="📻", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container {padding-top:3.2rem; padding-bottom:0.5rem}   /* 상단 바에 가려 위가 잘리지 않게 */
footer {visibility:hidden}
</style>
""", unsafe_allow_html=True)


# ── 데이터 ────────────────────────────────────────────

@st.cache_resource(show_spinner="모델 불러오는 중…")
def load_models(llm_mode):
    return load_classifier(), Extractor(mode=llm_mode)


def wav_seconds(path):
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error):
        return 2.0


@st.cache_data
def load_rows():
    stt = {r["file"]: r["hyp"] for r in read_csv(STT_CSV) if not r.get("error")}
    scenes = {}
    for r in sorted(read_csv(SCRIPT_CSV), key=lambda r: r["file"]):
        scenes.setdefault(r["file"][:3], []).append({
            "file": r["file"], "script": r["text"], "stt": stt.get(r["file"], r["text"]),
            "sec": wav_seconds(REC_DIR / r["file"]),
        })
    return scenes


@st.cache_data
def load_titles():
    """공개용 대본의 제목을 쓴다 (원본 radio_script.md 제목에는 '– 충돌'처럼 답이 들어 있음)."""
    titles = {}
    for path in (SCRIPT_MD, PUBLIC_MD):  # 공개용이 있으면 덮어쓴다
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^##\s*(S\d+)\.\s*(.+)$", line)
            if m:
                titles[m.group(1)] = m.group(2).strip()
    return titles


pretty, explain = anim.pretty, anim.explain


SCENES = load_rows()
TITLES = load_titles()


LLM_CSV = BASE / "llm_results.csv"         # llm_post.py로 미리 돌린 LLM 결과
FROM_FILE = "LLM 결과 파일 (llm_results.csv)"


@st.cache_data
def load_llm_results(mtime):
    """mtime: 파일이 바뀌면 다시 읽도록 캐시 키로 쓴다."""
    return {r["file"]: r for r in read_csv(LLM_CSV)} if LLM_CSV.exists() else {}


@st.cache_data(show_spinner="장면 분석 중…")
def scene_timeline(scene, source, llm_mode, mtime=0):
    if source == FROM_FILE:  # 모델 없이 파일 값으로 DB 저장·판정만 (빠름)
        return anim.build_timeline(None, None, scene, SCENES[scene], source, explain, pre=load_llm_results(mtime))
    clf, ext = load_models(llm_mode)
    return anim.build_timeline(clf, ext, scene, SCENES[scene], source, explain)


@st.cache_resource(show_spinner=False)
def prewarm(source, llm_mode, mtime=0):
    """발표 전에 모든 장면을 미리 분석해 둔다 (서버당 한 번). 발표 중 장면을 바꿔도 기다리지 않게."""
    bar = st.progress(0.0, text="시스템 준비 중: 장면을 미리 분석하고 있습니다…")
    for k, s in enumerate(SCENES, 1):
        scene_timeline(s, source, llm_mode, mtime)
        bar.progress(k / len(SCENES), text=f"시스템 준비 중… {s} ({k}/{len(SCENES)})")
    bar.empty()
    return True


# ── 사이드바: 설정 ────────────────────────────────────

with st.sidebar:
    st.header("⚙️ 설정")
    view = st.radio("보기", ["장면 하나", "연속 재생 (영상용)"],
                    help="연속 재생: 고른 장면을 순서대로 이어서 재생 (장면마다 상황 소개 → 무전 → 다음 장면, 끝에 결과 요약)")
    label = lambda s: f"{s} · {TITLES.get(s, '')}"  # noqa: E731
    if view == "장면 하나":
        picked = [st.selectbox("시나리오", list(SCENES), format_func=label)]
    else:
        picked = st.multiselect("장면 (고른 순서대로 재생)", list(SCENES),
                                default=[s for s in DEMO_DEFAULT if s in SCENES], format_func=label)
        if not picked:
            st.warning("장면을 하나 이상 고르세요.")
            st.stop()
    source = st.radio("입력", [FROM_FILE, "대본 문장", "Whisper STT 결과"],
                      help="LLM 결과 파일: llm_post.py로 미리 돌려 검토한 결과로 시연 (기본, 빠름)\n"
                           "대본 문장 / Whisper: 화면에서 직접 CLS·추출(LLM 포함)을 돌림")
    mtime = LLM_CSV.stat().st_mtime if LLM_CSV.exists() else 0
    if source == FROM_FILE:
        llm_mode = "off"
        res = load_llm_results(mtime)
        if not res:
            st.error("llm_results.csv가 없습니다. 먼저 python llm_post.py 를 실행하세요.")
            st.stop()
        used = sum(1 for r in res.values() if r.get("llm_raw"))
        agree = sum(1 for r in res.values() if r.get("llm_check") == "일치")
        st.caption(f"📄 {len(res)}건 · LLM 사용 {used}건 · 규칙·LLM 일치 {agree}건")
        if used == 0:
            st.warning("이 파일에는 LLM 결과가 없습니다 (규칙만으로 만든 파일). python llm_post.py 로 다시 만드세요.")
    else:
        llm_mode = st.radio("LLM (Qwen, 로컬)", list(LLM_MODES), format_func=LLM_MODES.get,
                            help="모든 문장: 규칙과 LLM이 각각 해석해 서로 다르면 확인 필요로 올림 (처음 준비가 오래 걸림)")
    st.caption("분석: 팀 CLS → 설비·행위 추출 → 팀 DB(설비 상태) → 판정. 결과는 화면의 DB 적재 패널에 그대로 보입니다.")


# ── 화면 ──────────────────────────────────────────────

prewarm(source, llm_mode, mtime)
scenes = [{"scene": s, "title": TITLES.get(s, ""), "story": STORY.get(s, ""),
           "timeline": scene_timeline(s, source, llm_mode, mtime)} for s in picked]
st.iframe(anim.render(scenes), height=800)
