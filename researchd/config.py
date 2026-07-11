"""Global config loading (config.yaml at repo root)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    'llm': {
        'base_url': 'http://localhost:1234/v1',
        'model': '',  # empty = first model LM Studio reports
        'max_tokens': 2048,
        'timeout_seconds': 180,
        'temperature': 0.3,
    },
    'missions_dir': 'missions',
    'state_db': 'state/researchd.db',
    'defaults': {
        'max_questions_per_night': 3,
        'max_sources_per_question': 5,
        'max_queries_per_question': 3,
        'max_new_frontier_items': 5,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    path = ROOT / 'config.yaml'
    user_cfg = {}
    if path.exists():
        user_cfg = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    cfg = _merge(DEFAULTS, user_cfg)
    cfg['missions_dir'] = str(ROOT / cfg['missions_dir'])
    cfg['state_db'] = str(ROOT / cfg['state_db'])
    # Single home for non-DB state (lockfile, active-mission pointer)
    cfg['state_dir'] = str(Path(cfg['state_db']).parent)
    return cfg
