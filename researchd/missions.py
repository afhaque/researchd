"""Mission management and the frontier file.

Red-team rule: the LLM never rewrites frontier.md. Python parses it into
items, the LLM proposes ops ({"close": [ids], "add": [texts]}), Python
validates and applies them, and a dated backup is taken before every write.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .util import atomic_write, now_iso, slugify, today_str

SCHEMA_TEMPLATE = """# Wiki Schema — {name}

Conventions for the LLM-maintained wiki (Karpathy LLM Wiki pattern).

## Layers
- `raw/` — fetched source snapshots. Immutable; never edited.
- `*.md` pages — LLM-drafted, code-finalized wiki pages.
- `index.md`, `log/` — maintained by code only. Never edited by the LLM.

## Page conventions
- One page per research question, named by slug.
- Frontmatter (written by code): title, date, run_id, question_id, tags.
- Body references sources as [S1], [S2]; the Sources section is appended
  by code from the actually-fetched URLs. Never write bare URLs in the body.
- Link related concepts with [[wikilinks]]. Prefer existing page names.

## Evidence levels (medical/scientific missions)
Tag claims: [RCT], [meta-analysis], [review], [observational], [anecdote].
"""

FRONTIER_TEMPLATE = """# Frontier — {name}

Open research questions. Maintained by researchd; edit by hand to steer.

## Open

{questions}

## Closed
"""


@dataclass
class FrontierItem:
    qid: int
    text: str
    closed: bool


class Frontier:
    LINE_RE = re.compile(r'^- \[( |x)\] Q(\d+): (.+)$')

    def __init__(self, path: Path):
        self.path = path
        self.items: list[FrontierItem] = []
        self._parse()

    def _parse(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding='utf-8').splitlines():
            m = self.LINE_RE.match(line.strip())
            if m:
                self.items.append(FrontierItem(
                    qid=int(m.group(2)),
                    text=m.group(3).strip(),
                    closed=m.group(1) == 'x',
                ))

    def open_items(self) -> list[FrontierItem]:
        return [i for i in self.items if not i.closed]

    def apply_ops(self, ops: dict, max_new: int) -> dict:
        """Validate and apply LLM-proposed ops. Returns what was applied."""
        applied = {'closed': [], 'added': []}
        open_ids = {i.qid for i in self.open_items()}
        for qid in ops.get('close', []) or []:
            if isinstance(qid, int) and qid in open_ids:
                next(i for i in self.items if i.qid == qid).closed = True
                applied['closed'].append(qid)
        next_id = max((i.qid for i in self.items), default=0) + 1
        for text in (ops.get('add', []) or [])[:max_new]:
            if isinstance(text, str) and text.strip():
                self.items.append(FrontierItem(next_id, text.strip(), False))
                applied['added'].append(next_id)
                next_id += 1
        return applied

    def save(self, mission_name: str) -> None:
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(f'.{today_str()}.bak'))
        open_lines = '\n'.join(
            f'- [ ] Q{i.qid}: {i.text}' for i in self.items if not i.closed)
        closed_lines = '\n'.join(
            f'- [x] Q{i.qid}: {i.text}' for i in self.items if i.closed)
        content = FRONTIER_TEMPLATE.format(name=mission_name, questions=open_lines)
        if closed_lines:
            content += closed_lines + '\n'
        atomic_write(self.path, content)


class Mission:
    def __init__(self, path: Path):
        self.path = path
        self.slug = path.name
        cfg_path = path / 'mission.yaml'
        if not cfg_path.exists():
            raise FileNotFoundError(f'no mission.yaml in {path}')
        self.cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8'))
        self.name = self.cfg.get('name', self.slug)

    @property
    def vault_path(self) -> Path:
        vp = self.cfg.get('vault_path')
        return Path(vp) if vp else self.path / 'wiki'

    @property
    def adapters(self) -> list[str]:
        return self.cfg.get('adapters', ['mock'])

    def budgets(self, defaults: dict) -> dict:
        return {**defaults, **(self.cfg.get('budgets') or {})}

    def frontier(self) -> Frontier:
        return Frontier(self.path / 'frontier.md')


def missions_root(cfg: dict) -> Path:
    return Path(cfg['missions_dir'])


def create_mission(cfg: dict, name: str, adapters: list[str],
                   questions: list[str]) -> Mission:
    slug = slugify(name)
    path = missions_root(cfg) / slug
    if path.exists():
        raise FileExistsError(f'mission {slug!r} already exists')
    path.mkdir(parents=True)
    (path / 'wiki' / 'raw').mkdir(parents=True)
    atomic_write(path / 'mission.yaml', yaml.safe_dump({
        'name': name,
        'created': now_iso(),
        'vault_path': None,  # null = wiki/ inside this mission dir
        'adapters': adapters,
        'budgets': {},
    }, sort_keys=False))
    atomic_write(path / 'schema.md', SCHEMA_TEMPLATE.format(name=name))
    seeds = questions or ['(seed this frontier with your first research question)']
    frontier = Frontier(path / 'frontier.md')
    frontier.items = [FrontierItem(n, q, False)
                      for n, q in enumerate(seeds, start=1)]
    frontier.save(name)
    return Mission(path)


def list_missions(cfg: dict) -> list[Mission]:
    root = missions_root(cfg)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if (p / 'mission.yaml').exists():
            out.append(Mission(p))
    return out


def _active_pointer(cfg: dict) -> Path:
    return Path(cfg['state_dir']) / 'active_mission'


def set_active(cfg: dict, slug: str) -> None:
    if not (missions_root(cfg) / slug / 'mission.yaml').exists():
        raise FileNotFoundError(f'no such mission: {slug}')
    atomic_write(_active_pointer(cfg), slug)


def get_active(cfg: dict) -> str | None:
    p = _active_pointer(cfg)
    return p.read_text(encoding='utf-8').strip() if p.exists() else None
