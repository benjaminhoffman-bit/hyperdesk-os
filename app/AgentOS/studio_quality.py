"""Local studio quality settings, edit measurements and retimed captions."""
from pathlib import Path

MODEL = Path(__file__).resolve().parent.parent / 'workspace/hyperdesk-media/whisper-large-v3'


def model_ready():
    return all((MODEL / name).is_file() for name in
               ('model.bin', 'config.json', 'tokenizer.json', 'preprocessor_config.json', 'vocabulary.json'))


def caption_cues(rows, spans, lengths=None):
    """Map original word timestamps to the edited timeline, never across a cut."""
    cues, offset = [], 0.
    for index, (start, end) in enumerate(spans):
        group = []
        def flush():
            if group:
                cues.append((group[0][0], group[-1][1], ' '.join(w[2] for w in group)))
                group.clear()
        for row in rows:
            words = row.get('words') or [{'start': row['start'], 'end': row['end'], 'text': row['text']}]
            for word in words:
                left, right = max(start, word['start']), min(end, word['end'])
                if right <= left:
                    continue
                left, right = offset + left-start, offset + right-start
                text = str(word['text']).replace('-->', '→').replace('\n', ' ').strip()
                if not text:
                    continue
                if group and (len(group) >= 7 or right-group[0][0] > 3 or left-group[-1][1] > .5):
                    flush()
                group.append((left, right, text))
                if text.endswith(('.', '?', '!')):
                    flush()
        flush()
        offset += lengths[index] if lengths is not None else end-start
    return cues


def captions(rows, spans, path, lengths=None):
    def stamp(seconds):
        ms = max(0, round(seconds*1000))
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}.{ms%1000:03}'
    cues = caption_cues(rows, spans, lengths)
    # Escape text so browser captions cannot interpret transcript words as markup.
    import html
    path.write_text('WEBVTT\n\n' + '\n\n'.join(
        f'{stamp(s)} --> {stamp(e)}\n{html.escape(t)}' for s, e, t in cues) + '\n', encoding='utf-8')
    return len(cues)


def measurements(rows, duration, spans):
    words = [w for r in rows for w in r.get('words', [])]
    count = len(words) if words else sum(len(r['text'].split()) for r in rows)
    kept = sum(e-s for s, e in spans)
    return {'original_seconds': round(duration, 2), 'edited_seconds': round(kept, 2),
            'removed_seconds': round(max(0, duration-kept), 2),
            'kept_percent': round(100*kept/duration) if duration else 0,
            'words': count, 'words_per_minute': round(60*count/duration) if duration else 0,
            'uncertain_words': sum(w.get('probability', 1) < .6 for w in words)}
