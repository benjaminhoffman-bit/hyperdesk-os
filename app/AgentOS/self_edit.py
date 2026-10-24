"""Save the app's source before a user-requested change; never merge automatically."""
from pathlib import Path
import threading
import agent_os as a

LOCK = threading.Lock()
SOURCE = {'.py', '.pyw', '.html', '.js', '.css', '.md', '.toml', '.json', '.ps1',
          '.svg', '.png', '.ico', '.woff2', '.ttf', '.txt'}


def checkpoint(root=None):
    root = Path(root or a.ROOT)
    # Apply update leaves its merge staged while the tests run: a commit here would finish that merge untested
    # ponytail: a file check, not a lock; a sub-second window remains between it and the commit below
    if (root / '.git' / 'MERGE_HEAD').exists():
        raise ValueError('An update is being applied. Try again when it finishes. If none is running, an earlier '
                         f'one stopped halfway: run  git -C "{root}" merge --abort')
    def git(*args):
        code, output = a.run(['git', '-C', str(root), *args], 120)
        if code:
            raise ValueError('Could not save the current app version. ' + output[:300])
        return output
    paths = set(filter(None, git('diff', '--name-only', '-z', 'HEAD').split('\0')))
    untracked = set(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    paths.update(untracked)
    for name in paths:
        p = Path(name)
        research_doc = p.parts[0] in ('reports', 'research_notes') and p.suffix == '.md'
        if name in untracked and not research_doc and p.parts[0] not in ('assets', 'tests', 'docs', 'prompts') and p.suffix not in ('.py', '.pyw', '.js', '.html', '.css'):
            raise ValueError('An unexpected file needs review before saving the app version: ' + name)
        if (p.suffix.lower() not in SOURCE and name != '.gitignore') or any(
                part.lower() in ('state', 'knowledge', 'logs', 'node_modules') or part.startswith('.')
                for part in p.parts if part != '.gitignore'):
            raise ValueError('An unexpected file needs review before saving the app version: ' + name)
        if (root / p).is_symlink():
            raise ValueError('Linked files need review before saving the app version.')
    if paths:
        git('add', '--', *sorted(paths))
        git('-c', 'user.name=HyperDesk', '-c', 'user.email=hyperdesk@localhost', 'commit', '-q',
            '-m', 'Save HyperDesk version before requested app change')
    if git('status', '--porcelain').strip():
        raise ValueError('The app files changed while saving. Please try your request again.')
    return git('rev-parse', 'HEAD').strip()
