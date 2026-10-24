"""High-quality local speech worker. CUDA libraries are scoped to this process."""
import json
import os
import sys
from pathlib import Path
from faster_whisper import WhisperModel


def transcribe(source, model_name, cache, output):
    # Reuse verified CUDA 12/cuDNN 9 libraries without modifying the other environment.
    gpu = Path(model_name).is_dir()
    dll_handles = []
    if gpu and os.name == 'nt':
        import ctypes
        library = Path(__file__).resolve().parent.parent / 'ai-video-editor/.venv/Lib/site-packages/torch/lib'
        dll_handles.append(os.add_dll_directory(str(library)))
        for name in ('cublasLt64_12.dll', 'cublas64_12.dll', 'cudnn64_9.dll'):
            dll_handles.append(ctypes.WinDLL(str(library / name)))
    model = WhisperModel(model_name, device='cuda' if gpu else 'cpu',
                         compute_type='float16' if gpu else 'int8', cpu_threads=4,
                         download_root=cache, local_files_only=True)
    segments, info = model.transcribe(source, beam_size=5, vad_filter=True, word_timestamps=True,
                                     vad_parameters={'min_silence_duration_ms': 500, 'speech_pad_ms': 300},
                                     language='en', hallucination_silence_threshold=2,
                                     condition_on_previous_text=False)
    rows, words = [], []
    def flush():
        if words:
            rows.append({'id': len(rows), 'start': words[0].start, 'end': words[-1].end,
                         'text': ''.join(w.word for w in words).strip(),
                         'words': [{'start': w.start, 'end': w.end, 'text': w.word.strip(),
                                    'probability': w.probability} for w in words]})
            words.clear()
    for segment in segments:
        for word in segment.words or []:
            if words and (word.start - words[-1].end > 0.8 or word.end - words[0].start > 8):
                flush()
            words.append(word)
            if word.word.rstrip().endswith(('.', '?', '!')):
                flush()
        flush()
    Path(output).write_text(json.dumps({'language': info.language, 'segments': rows,
                                     'model': 'large-v3' if gpu else model_name,
                                     'device': 'cuda' if gpu else 'cpu', 'beam_size': 5}), encoding='utf-8')


if __name__ == '__main__':
    transcribe(*sys.argv[1:5])
