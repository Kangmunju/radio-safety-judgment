"""STT

사용법:
    python stt.py                                  # 전체 101건 받아쓰기 + 평가
    python stt.py --scene S01                      # S01 장면만
    python stt.py --model openai/whisper-small     # 다른 모델
    python stt.py --no-prompt                      # 용어 프롬프트 없이 (비교용)
    python stt.py --file recordings/S01_04_A.wav   # 파일 하나만 받아쓰기

"""
import argparse
import csv
import re
import time
import wave
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

BASE = Path(__file__).resolve().parent
REC_DIR = BASE / "recordings"
REF_CSV = BASE / "recordings.csv"
OUT_CSV = BASE / "stt_results.csv"
SAMPLE_RATE = 16000

DEFAULT_MODEL = "openai/whisper-large-v3"  # 빠르게 하려면 openai/whisper-large-v3-turbo

# 현장 용어를 앞 문맥으로 넣어 주면 Whisper가 해당 단어로 받아쓸 확률이 올라간다.
TERM_PROMPT = (
    "제철소 원료 설비 무전. 운전실, 컨베이어, 파쇄기, 크러셔, 송풍기, 블로워, "
    "낙광, LOTO, 아이들러, 스크레이퍼, 슈트, 테일 풀리, 라이너, 사행, 시운전, 작업허가, 복창."
)


def pick_device():
    if torch.cuda.is_available():
        return "cuda", torch.float16
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def load_wav(path):
    """16kHz 모노 16bit wav → float32 [-1, 1]."""
    with wave.open(str(path)) as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: 16kHz 모노 16bit wav만 지원합니다")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


class STT:
    def __init__(self, model_id=DEFAULT_MODEL, prompt=TERM_PROMPT, device=None):
        self.device, self.dtype = (device, torch.float32) if device else pick_device()
        print(f"모델 로드: {model_id} ({self.device}, {str(self.dtype).split('.')[-1]})")
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_id, dtype=self.dtype, low_cpu_mem_usage=True
        ).to(self.device).eval()
        self.prompt = prompt
        self.prompt_ids = (
            torch.tensor(self.processor.get_prompt_ids(prompt), device=self.device) if prompt else None
        )

    @torch.inference_mode()
    def transcribe_batch(self, audios):
        """audios: 16kHz float32 numpy 배열 리스트 (각 30초 이하) → 텍스트 리스트."""
        inputs = self.processor(
            audios, sampling_rate=SAMPLE_RATE, return_tensors="pt", return_attention_mask=True
        )
        kwargs = dict(language="ko", task="transcribe", max_new_tokens=128,
                      attention_mask=inputs.attention_mask.to(self.device))
        if self.prompt_ids is not None:
            kwargs["prompt_ids"] = self.prompt_ids
        ids = self.model.generate(inputs.input_features.to(self.device, self.dtype), **kwargs)
        texts = self.processor.batch_decode(ids, skip_special_tokens=True)
        # transformers 버전에 따라 결과 앞에 프롬프트가 붙어 나오는 경우가 있어 제거
        if self.prompt:
            texts = [t.split(self.prompt, 1)[-1] for t in texts]
        return [t.strip() for t in texts]

    def transcribe(self, audio):
        if isinstance(audio, (str, Path)):
            audio = load_wav(audio)
        return self.transcribe_batch([audio])[0]


# ── 후처리 ────────────────────────────────────────────
# Whisper는 "3번"을 "삼 번"으로, "LOTO"를 "로또"로 쓰는 등 표기가 흔들린다.
# 뒤 단계(설비·행위 추출)가 쉽도록 현장 표기로 맞춘다.

_NUM = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
# "이번에는/이번 주/이번 달" 같은 일반 표현은 숫자로 바꾸지 않는다
_NUM_RE = re.compile(r"(?<![가-힣])([일이삼사오육칠팔구])\s*(번|호기|호|컨)(?!째|에|엔|\s*(주|달|차례))")
_LOTO_RE = re.compile(r"로[토또터도]|LOTTO|lot[oa]", re.IGNORECASE)
# 문장 맨 앞의 호출 부호: "삐," → "B,"
# 뒤에 쉼표·공백이 있어야 호출로 본다 ("비상 정지", "시운전"을 B·C로 바꾸지 않도록)
_CALL_RE = re.compile(r"^\s*(에이|비|삐|씨|시)(?:\s*[,.]\s*|\s+)(?=\S)")
_CALL = {"에이": "A", "비": "B", "삐": "B", "씨": "C", "시": "C"}


_EQUIP = r"(?=\s*(컨베이어|컨|벨트|송풍기|블로워|라인))"
# "일본/사방/칠방/삼분 컨베이어" → N번 컨베이어 (설비 이름 앞에서만)
_NUM_MISHEAR_RE = re.compile(r"(?<![가-힣])([일이삼사오육칠팔구]|\d)\s*(본|방|반|분|번)" + _EQUIP)
# "일 오기/일로기 파쇄기" → 1호기 (ㅎ 탈락)
_HOGI_RE = re.compile(r"(?<![가-힣])([일이삼])\s*[오옥로]\s*기")


def _digit(s):
    return s if s.isdigit() else str(_NUM[s])


def postprocess(text):
    text = _NUM_MISHEAR_RE.sub(lambda m: f"{_digit(m.group(1))}번", text)
    text = _HOGI_RE.sub(lambda m: f"{_NUM[m.group(1)]}호기", text)
    text = _NUM_RE.sub(lambda m: f"{_NUM[m.group(1)]}{m.group(2)}", text)
    text = re.sub(r"(\d)\s+(번|호기|호)", r"\1\2", text)
    text = _LOTO_RE.sub("LOTO", text)
    text = _CALL_RE.sub(lambda m: f"{_CALL[m.group(1)]}, ", text)
    return text.strip()


# ── 평가 ──────────────────────────────────────────────

def normalize(text):
    """비교용: 공백·문장부호 제거, 영문 대문자화."""
    return re.sub(r"[^\w]", "", text).upper()


# 녹음에서는 대본의 "B," 대신 "창호야," 처럼 이름으로 부른 경우가 많아
# 후처리 CER에서는 문장 앞 호출(수신자 지정)을 양쪽 다 빼고 비교한다.
_ADDRESS_RE = re.compile(r"^\s*([A-C]|[가-힣]{1,3}[야아])\s*[,.]?\s+")


def strip_address(text):
    return _ADDRESS_RE.sub("", text, count=1)


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def load_refs(scene=None):
    with REF_CSV.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if scene:
        rows = [r for r in rows if r["scenario"] == scene.upper()]
    return rows


def evaluate(stt, rows, batch_size):
    results = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        audios = [load_wav(REC_DIR / r["file"]) for r in chunk]
        t0 = time.perf_counter()
        hyps = stt.transcribe_batch(audios)
        per_file = (time.perf_counter() - t0) / len(chunk)
        for r, audio, hyp in zip(chunk, audios, hyps):
            post = postprocess(hyp)
            # raw: Whisper 출력 그대로 / post: 양쪽 다 후처리 후 비교 ("일번"↔"1번" 같은 표기 차이 제외)
            ref_raw, hyp_raw = normalize(r["text"]), normalize(hyp)
            ref_pp = normalize(strip_address(postprocess(r["text"])))
            hyp_pp = normalize(strip_address(post))
            err_raw, err_pp = edit_distance(ref_raw, hyp_raw), edit_distance(ref_pp, hyp_pp)
            res = {
                "file": r["file"], "scenario": r["scenario"], "radio": r["radio"],
                "ref": r["text"], "hyp": hyp, "hyp_post": post,
                "chars": len(ref_raw), "errors": err_raw,
                "cer": round(err_raw / max(len(ref_raw), 1), 4),
                "chars_post": len(ref_pp), "errors_post": err_pp,
                "cer_post": round(err_pp / max(len(ref_pp), 1), 4),
                "exact_post": int(ref_pp == hyp_pp),
                "audio_sec": round(len(audio) / SAMPLE_RATE, 2),
                "infer_sec": round(per_file, 3),
            }
            results.append(res)
            mark = "✓" if res["exact_post"] else f"CER {res['cer_post']:.2f}"
            print(f"[{mark:>8}] {r['file']:<14} {post}")
            if not res["exact_post"]:
                print(f"{'':11}{'정답':<14} {r['text']}")
    return results


def summarize(results):
    def cer(rows, sfx=""):
        return sum(r["errors" + sfx] for r in rows) / sum(r["chars" + sfx] for r in rows)

    audio = sum(r["audio_sec"] for r in results)
    infer = sum(r["infer_sec"] for r in results)
    print("\n── 결과 요약 ──")
    print(f"파일 수          : {len(results)}")
    print(f"CER (원본)       : {cer(results):.2%}")
    print(f"CER (후처리)     : {cer(results, '_post'):.2%}")
    print(f"완전 일치(후처리): {sum(r['exact_post'] for r in results)}/{len(results)}")
    print(f"처리 시간        : {infer:.1f}초 / 오디오 {audio:.1f}초 (RTF {infer / audio:.3f})")
    by_radio = {}
    for r in results:
        e, c = by_radio.get(r["radio"], (0, 0))
        by_radio[r["radio"]] = (e + r["errors_post"], c + r["chars_post"])
    print("무전기별 CER(후처리): " +", ".join(f"{k} {e / c:.2%}" for k, (e, c) in sorted(by_radio.items())))


def main():
    p = argparse.ArgumentParser(description="무전 녹음 Whisper STT")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Hugging Face 모델 ID (기본 {DEFAULT_MODEL})")
    p.add_argument("--scene", help="이 장면만 (예: S01)")
    p.add_argument("--file", help="wav 파일 하나만 받아쓰기 (평가 없음)")
    p.add_argument("--no-prompt", action="store_true", help="용어 프롬프트 끄기")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--cpu", action="store_true", help="CPU 강제 사용")
    p.add_argument("--out", default=str(OUT_CSV), help="결과 CSV 경로")
    args = p.parse_args()

    stt = STT(args.model, prompt=None if args.no_prompt else TERM_PROMPT, device="cpu" if args.cpu else None)

    if args.file:
        print(postprocess(stt.transcribe(args.file)))
        return

    results = evaluate(stt, load_refs(args.scene), args.batch_size)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    summarize(results)
    print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
