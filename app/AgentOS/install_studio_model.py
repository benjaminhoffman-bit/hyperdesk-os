"""Install the pinned official Whisper large-v3 conversion; no package/global changes."""
import hashlib
from pathlib import Path
import urllib.request

REVISION = 'edaa852ec7e145841d8ffdb056a99866b5f0a478'  # released 2023-11-23
DEST = Path(__file__).resolve().parent.parent / 'workspace' / 'hyperdesk-media' / 'whisper-large-v3'
FILES = {
    'config.json': (2394, 'sha1', '75336feae814999bae6ccccdecf177639ffc6f9d'),
    'model.bin': (3087284237, 'sha256', '69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1'),
    'preprocessor_config.json': (340, 'sha1', '931c77a740890c46365c7ae0c9d350ba3cca908f'),
    'tokenizer.json': (2480617, 'sha1', '3a5e2ba63acdcac9a19ba56cf9bd27f185bfff61'),
    'vocabulary.json': (1068114, 'sha1', '0adcd01e7c237205d593b707e66dd5d7bc785d2d'),
}


def verified(path, size, algorithm, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    h = hashlib.new(algorithm)
    if algorithm == 'sha1':
        h.update(f'blob {size}\0'.encode())
    with path.open('rb') as src:
        for data in iter(lambda: src.read(4*1024*1024), b''):
            h.update(data)
    return h.hexdigest() == digest


def install():
    DEST.mkdir(parents=True, exist_ok=True)
    for name, (size, algorithm, digest) in FILES.items():
        path = DEST / name
        if verified(path, size, algorithm, digest):
            print(name + ': verified', flush=True)
            continue
        partial = path.with_suffix(path.suffix + '.download')
        url = f'https://huggingface.co/Systran/faster-whisper-large-v3/resolve/{REVISION}/{name}'
        print('Downloading ' + name, flush=True)
        with urllib.request.urlopen(url, timeout=120) as source, partial.open('wb') as out:
            for data in iter(lambda: source.read(4*1024*1024), b''):
                out.write(data)
        if not verified(partial, size, algorithm, digest):
            raise RuntimeError('Checksum mismatch: ' + name + '. Existing model has not been replaced.')
        partial.replace(path)
        print(name + ': installed and verified', flush=True)
    print('Model installation complete: ' + str(DEST), flush=True)


if __name__ == '__main__':
    install()
