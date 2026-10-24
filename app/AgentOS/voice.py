"""Local speech-to-text for HyperDesk's mic buttons. Runs under Open WebUI's Python, which already ships faster-whisper
(agent_os.voice_python finds it), so nothing else is installed. On the CPU: no VRAM taken from models or games.

  python voice.py <audio file> <model> <models folder>   one clip; prints {"text", "language", "seconds"}
  python voice.py --serve <model> <models folder>        resident: prints {"ready": true}, then one JSON reply per
                                                          {"file": ...} line on stdin, until stdin closes

Measured 2026-10-05 (small.en, int8, CPU): importing takes ~6 s and loading ~2 s, a 4-second clip ~1.5 s. So
HyperDesk keeps one --serve process and starts it the moment recording starts. The model downloads into the models
folder on first use (~480 MB).
"""
import json
import sys
import time

from faster_whisper import WhisperModel


def transcribe(model: WhisperModel, english: bool, audio: str) -> dict:
    start = time.monotonic()
    # vad_filter drops the silence around the words; English-only models skip language detection
    segments, info = model.transcribe(audio, beam_size=1, vad_filter=True, language="en" if english else None)
    return {"text": " ".join(s.text.strip() for s in segments).strip(), "language": info.language,
            "seconds": round(time.monotonic() - start, 1)}


def load(name: str, root: str) -> WhisperModel:
    return WhisperModel(name, device="cpu", compute_type="int8", download_root=root)


if __name__ == "__main__":
    if sys.argv[1] == "--serve":
        name, root = sys.argv[2:4]
        model = load(name, root)
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            try:
                reply = transcribe(model, name.endswith(".en"), json.loads(line)["file"])
            except Exception as e:  # one bad clip must not end the worker
                reply = {"error": repr(e)[:300]}
            print(json.dumps(reply), flush=True)
    else:
        audio, name, root = sys.argv[1:4]
        print(json.dumps(transcribe(load(name, root), name.endswith(".en"), audio)))
