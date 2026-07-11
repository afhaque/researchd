# researchd

Nightly local-LLM research agent. A deterministic Python pipeline runs bounded
research cycles against a local model (LM Studio), pulls sources per mission
(Tavily, PubMed), and maintains a Karpathy-style LLM Wiki — plain markdown
with wikilinks — that Obsidian reads directly.

**Design law (from red-teaming):** the LLM proposes, Python disposes. Every
canonical file (frontier, index, log) is written only by deterministic code
from validated LLM proposals. The LLM never rewrites state files.

## Layout

```
config.yaml            machine config: LM Studio endpoint, budgets
researchd/             the package (CLI, pipeline, adapters, wiki writer)
missions/<slug>/       one directory per research theme
  mission.yaml         vault path, adapters, budget overrides
  frontier.md          open-questions checklist — the multi-night state
  schema.md            wiki conventions the LLM follows
  wiki/                the wiki (index.md, pages, log/, raw/, nightly-report.md)
state/                 SQLite dedup/run state, active-mission pointer, lock
scheduler/             cron / systemd / Task Scheduler examples
```

## Setup on the GPU box

1. **LM Studio**: load your model (e.g. Qwen3-32B Q4), then enable the local
   server (Developer tab → Start Server, or `lms server start` for headless).
   Default endpoint `http://localhost:1234/v1` is already in `config.yaml`;
   set `llm.model` to the model id LM Studio shows, or leave empty to use the
   first loaded model. Enable JIT loading / keep-alive so the model is
   resident at 1 AM.
2. **Install**: `pip install pyyaml requests` (or `pip install -e .`).
3. **Keys**: `export TAVILY_API_KEY=...` for the tavily adapter. PubMed
   (E-utilities) needs no key at low volume.

## Usage

```bash
# create a mission (becomes active), seed it with questions
python -m researchd mission new "Autism early interventions" \
  --adapters pubmed,tavily \
  --question "What interventions have RCT evidence for children under 5?"

python -m researchd mission list
python -m researchd mission use autism-early-interventions

# point the wiki at your real vault (optional — default is missions/<slug>/wiki)
#   edit missions/<slug>/mission.yaml → vault_path: /path/to/Vault/Research/Autism

# nightly cycle (this is what cron runs)
python -m researchd run --max-minutes 405

# end-to-end smoke test — no GPU, no keys, mock LLM + mock search
python -m researchd run --dry-run --max-minutes 5
```

Each night ends with `wiki/nightly-report.md` — skim it in Obsidian in the
morning and steer by editing `frontier.md` by hand (add/close questions).

## Scheduling

See `scheduler/`. Run window 1:00 AM with `--max-minutes 405` ≈ hard stop by
7:45 AM. The deadline is monotonic (DST-safe) and checked at loop boundaries
only, so shutdown is always graceful. A lockfile prevents overlapping runs.

## Vault sync warning

If the vault syncs (Obsidian Sync / iCloud), concurrent 3 AM writes can
conflict with stale copies from other devices. Safest: keep the wiki in a
git-synced folder, or leave `vault_path` local and pull pages into your main
vault manually. Never place `state/` inside a synced folder.

## Adding a search adapter

Subclass `SearchAdapter` in `researchd/adapters/`, return `SearchResult`s
with content included, register it in `get_adapter()`, and list it in the
mission's `adapters:`.
