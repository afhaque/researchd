# researchd

A nightly research agent that runs on your own machine. You give it a research
theme and a few starting questions; it spends the overnight hours reading
sources with a **local** LLM (via LM Studio) and compiles what it finds into a
growing, interlinked wiki of markdown files that [Obsidian](https://obsidian.md)
reads directly. Leave it running and the wiki gets richer every night.

It is deliberately **not** an autonomous agent turned loose on a goal. Python
owns the loop and the model is called only for small, bounded jobs — the design
that keeps a modest local model reliable over long unattended runs (see
[How it works](#how-it-works)).

The model backend is swappable: run against the **Anthropic cloud** now (the
default — a cheap model grades sources, a strong one writes pages) and switch to
a **local model** later by editing one line in `config.yaml`. See
[Choosing a model backend](#choosing-a-model-backend).

---

## What it does

- **Runs a research theme over many nights.** Each theme is a *mission*: a
  folder with a config, a list of open questions (`frontier.md`), and its wiki.
  You can keep several missions and point the agent at whichever one is active.
- **Pulls real sources per mission.** Web search via [Tavily](https://tavily.com)
  and biomedical literature via [PubMed](https://pubmed.ncbi.nlm.nih.gov/)
  (NCBI E-utilities) today; the adapter interface is a few lines to extend.
- **Compiles, doesn't just retrieve.** Following
  [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f),
  it reads each source, extracts what matters, and files it into an evolving set
  of wiki pages with `[[wikilinks]]` — so knowledge accumulates instead of being
  re-derived on every question.
- **Stays bounded and safe to leave alone.** Hard budgets (how many questions
  and sources per night), a wall-clock deadline with graceful shutdown, a
  lockfile against overlapping runs, cross-night deduplication so it never
  re-reads the same article, and atomic writes so a crash never corrupts a file.

The human's job is what Karpathy describes: curate sources, ask good questions,
steer. The agent does the reading, extracting, cross-referencing, and
bookkeeping.

---

## Quick start

```bash
# 1. Install (only two dependencies)
pip install pyyaml requests          # or: pip install -e .

# 2. Try the whole cycle right now — no GPU, no API keys, no model needed.
#    Uses a mock model + mock search, fully sandboxed (writes to wiki-dryrun/).
python -m researchd mission new "Demo" --question "What is this thing?"
python -m researchd run --dry-run --max-minutes 5
#    → open missions/demo/wiki-dryrun/ to see the pages, index, and log it made
```

That dry run touches nothing real and needs nothing installed beyond the two
Python packages — it's the fastest way to see the shape of the output.

---

## Choosing a model backend

`config.yaml` → `llm.provider` selects where the model runs. Switching is a
one-line edit; nothing else in the pipeline changes.

**Anthropic cloud (`provider: anthropic`, the default).** Runs immediately with
just an API key — no GPU, no model download. It uses two models by cost:
Claude Haiku 4.5 for the high-volume grading and query steps (cheap), and Claude
Sonnet 5 for synthesis (strong). Both are set under `llm.anthropic`; change
`default_model` to `claude-opus-4-8` if you want maximum synthesis quality, or
point every step at one model. Set the key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Cost scales with your per-night budgets — grading is one Haiku call per source,
synthesis one Sonnet call per page. Start with the default budgets and watch the
first few `nightly-report.md` files before turning them up.

**Local model (`provider: openai`).** Runs against any OpenAI-compatible server
— LM Studio, Ollama, llama.cpp, vLLM. No token cost, full privacy. Set
`provider: openai`, load a model (e.g. Qwen3-32B or Qwen3.5 at Q4) in LM Studio,
start its server (Developer tab → *Start Server*, or `lms server start`
headless), and turn on JIT loading / keep-alive so the model is resident when
the job fires at 1 AM. The default endpoint `http://localhost:1234/v1` is in
`config.yaml` under `llm.openai`; leave `model` empty to use the first loaded
model. One loaded model serves every step (no per-step routing locally).

The recommended path: prove the pipeline end-to-end on Anthropic cloud, then
flip to local once you're happy with the plumbing.

## Setup for real runs

1. **Pick a backend** — see [Choosing a model backend](#choosing-a-model-backend)
   above. Cloud needs only `ANTHROPIC_API_KEY`; local needs LM Studio running.
2. **Keys** — `export TAVILY_API_KEY=...` for the Tavily adapter. PubMed needs
   no key at low volume.
3. **Point the wiki at your vault** *(optional)* — by default a mission writes
   to `missions/<slug>/wiki/`. To write straight into your Obsidian vault, edit
   `missions/<slug>/mission.yaml` and set `vault_path: /path/to/Vault/Research/Autism`.
   (Read the [vault-sync warning](#vault-sync-warning) first.)

---

## How to initiate it

### Create and steer missions

```bash
# Create a mission (it becomes the active one) and seed its questions
python -m researchd mission new "Autism early interventions" \
  --adapters pubmed,tavily \
  --question "What interventions have RCT evidence for children under 5?" \
  --question "What do parent-mediated intervention studies show?"

python -m researchd mission list            # list missions; * marks the active one
python -m researchd mission use autism-early-interventions   # switch active mission
```

You steer at any time by hand-editing `missions/<slug>/frontier.md` — add a
question, or check one off to close it. The agent picks up your edits on the
next run.

### Run a night

```bash
# One research cycle against the active mission (this is what the scheduler runs)
python -m researchd run --max-minutes 405

# Or target a specific mission regardless of which is active
python -m researchd run --mission japanese-classical-pianists --max-minutes 405
```

Each run ends by writing `wiki/nightly-report.md`. Skim it in Obsidian over
coffee, adjust the frontier, and let the next night build on it.

### Schedule it

The `scheduler/` folder has ready examples for cron, systemd, and Windows Task
Scheduler. The essence (cron on the GPU box):

```cron
# 1:00 AM nightly; --max-minutes 405 hard-stops it by ~7:45 AM
0 1 * * * cd /path/to/ResearchWeb && python -m researchd run --max-minutes 405 >> state/cron.log 2>&1
```

The deadline is measured in monotonic time (so it's DST-safe) and checked only
at loop boundaries, so shutdown is always clean — a kill never lands mid-write.

---

## How it works

The core design decision is **who holds the loop**. In a typical "autonomous
agent" you hand a model a goal and some tools and let it decide every step —
what to search, when a result is good enough, when to write, when it's done.
Across a multi-hour unattended run a modest local model drifts: it loses track
of what it already did, re-searches, blends sources, fabricates citations to
fill gaps, and either stops early or never stops.

researchd inverts that. **Python holds the loop; the model is called only for
small, single-purpose jobs, each with fresh context, and hands control straight
back to code every time.** The model proposes; deterministic code disposes.
Every canonical file — the frontier, the index, the log — is written *only* by
Python from validated model output. The model never rewrites a state file.

One night runs like this:

1. **Pick** — Python reads `frontier.md` and takes the next few open questions
   (up to `max_questions_per_night`). *Code chooses, not the model.*
2. **Query** — for a question, one model call: "generate N search queries."
   Python runs them through the mission's adapters.
3. **Dedup** — Python drops any URL already seen on a prior night (tracked in
   SQLite) and caps the rest to `max_sources_per_question`, so it stops fetching
   the moment the budget is full and only ever ingests *new* material.
4. **Grade** — one model call **per source**, one at a time with fresh context,
   so it can't blend or drift: "is this relevant, summarize it, and quote one
   line that proves it." Python verifies the quote actually appears in the
   source and discards the source if it doesn't — a cheap fabrication guard.
5. **Synthesize** — one model call: "write a wiki page from these findings,"
   referencing sources as `[S1]`, `[S2]`. Python attaches the real source list,
   strips any URL the model invented, writes the page atomically, and rebuilds
   `index.md` and appends `log/<date>.md` itself.
6. **Update the frontier** — one model call returns JSON like
   `{"close": [2], "add": ["new follow-up question"]}`. Python validates the ops
   against known question IDs, applies them, and snapshots a dated backup of
   `frontier.md` before saving. The model never edits the file directly.

Then it repeats the next night. Because the wiki and the seen-URL log persist,
each run builds on the last: new sources, new and revised pages, a frontier that
grows with follow-ups and shrinks as questions get answered.

### Anatomy of a mission

```
missions/autism-early-interventions/
  mission.yaml          vault path, which adapters, budget overrides
  frontier.md           the open/closed question list — your steering surface
  schema.md             wiki conventions the model is told to follow
  frontier.<date>.bak   automatic backups taken before each frontier write
  wiki/                 (or your Obsidian vault, if vault_path is set)
    index.md            catalog of every page — rebuilt by code each run
    nightly-report.md   what happened last night
    log/2026-07-11.md   one append-only log file per night (sync-friendly)
    raw/                immutable snapshots of every source actually ingested
    q1-*.md, q2-*.md    the wiki pages, one per answered question
```

---

## A week of running it (illustrative)

This is a projection of how the pipeline behaves night to night, not captured
output — but it maps directly to the steps above. Say you seed the autism
mission with two questions and default budgets (3 questions, 5 sources each).

**Night 1.** Frontier has `Q1` and `Q2` open. The agent works both plus has
budget to spare. For `Q1` ("RCT evidence for under-5s") it generates queries,
pulls ~5 fresh PubMed/Tavily sources, grades them (say 4 survive), and writes
`q1-what-interventions-have-rct-evidence-for-children-under-5.md`, tagging claims
by evidence level per `schema.md`. Same for `Q2`. In the frontier step it
proposes closing nothing yet but *adds* two follow-ups the reading surfaced —
`Q3: "How durable are gains at 2-year follow-up?"` and
`Q4: "Which parent-training formats show the strongest effects?"`. Morning
report: *2 pages written from 8 sources; added Q3, Q4.* The `index.md` now lists
two pages; `raw/` holds 8 source snapshots.

**Night 2.** Frontier: `Q1`–`Q4` open. The agent picks the top three. `Q1` and
`Q2` are re-examined but *most of last night's sources dedup out* — it finds
only 1–2 genuinely new articles for `Q1`, folds them into the existing page (new
`[S]` entries, revised synthesis), and finds nothing new for `Q2`, so that page
is left as-is and the report notes "no new sources." It then does real work on
`Q3`, writing `q3-*.md`. Frontier step: proposes **closing `Q2`** (well
covered), adds `Q5: "Do effects generalize outside the clinic setting?"`.

**Night 3.** With `Q2` closed, the picked set is `Q3`, `Q4`, `Q5`. `Q4` gets its
page; `Q5` gets its page; `Q3` picks up a couple more sources. Pages are now
cross-linking — `q4-*.md` references `[[q1-...]]` because the model saw the
concept already had a page. The graph view in Obsidian starts to show hubs.

**Nights 4–5.** New sources per question taper off as the obvious literature is
exhausted; nights get quieter and the reports increasingly say "1 new source" or
"none." This is the signal the vein is running dry. You open the wiki over the
weekend, read the six or seven interlinked pages, and **steer**: close the
questions you consider answered, and hand-add a sharper one —
`Q9: "How does intervention intensity (hours/week) trade off against family burden?"`

**Night 6+.** The agent attacks your new question with fresh energy while the
existing wiki gives it context to link into. Over a couple of weeks you've
accumulated a small, dense, cited wiki on the topic — built almost entirely
while you were asleep, from sources you can audit page-by-page in `raw/`.

The rhythm is the point: **the agent does the tireless bookkeeping every night;
you drop in occasionally to read, prune, and aim it.**

---

## Vault-sync warning

If your Obsidian vault syncs across machines (Obsidian Sync, iCloud, Dropbox),
concurrent writes at 3 AM can collide with a stale copy from another device and
produce `... (conflicted copy).md` files. Safest options: keep the wiki in a
git-synced folder, or leave `vault_path` unset (write locally) and pull pages
into your main vault yourself. **Never** put `state/` inside a synced folder —
the SQLite DB must not be synced.

---

## Adding a search adapter

Subclass `SearchAdapter` in `researchd/adapters/`, return `SearchResult`s with
the content included, register it in `get_adapter()`, and list its name in a
mission's `adapters:`. Everything downstream — dedup, grading, synthesis — works
unchanged.

---

## Design notes

- **Dependencies:** `pyyaml` and `requests`. No LangChain, no framework, no
  server. The whole thing is a small readable Python package.
- **LLM API:** two backends behind one interface — the Anthropic Messages API
  (called directly over HTTPS, no SDK) and any OpenAI-compatible
  `/v1/chat/completions` endpoint (LM Studio, Ollama, llama.cpp, vLLM). Switch
  with `llm.provider`.
- **Reliability guards baked in:** bounded token/timeout on every call,
  defensive JSON parsing with one retry and a fallback, quote-verification
  against source text, atomic file writes, cross-night dedup, a run lockfile,
  and a preflight check that writes a `FAILED` report (instead of hanging) if
  the model server isn't up when the job fires.
```
