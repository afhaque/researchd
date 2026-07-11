"""The nightly pipeline. Deterministic outer loop; the LLM does five bounded
jobs with fresh context each call: pick nothing (Python picks), generate
queries, grade sources, synthesize a page, propose frontier ops.

Deadline is monotonic and checked only at loop boundaries, so a kill never
lands mid-write. A lockfile prevents overlapping runs.
"""

import fcntl
import time
import uuid
from pathlib import Path

import requests

from .adapters import get_adapter
from .llm import LLMError, make_llm
from .missions import Mission
from .state import State
from .util import today_str
from .wiki import (append_log, append_section, init_page, rebuild_index,
                   save_raw, write_report)

SOURCE_CHAR_BUDGET = 6000  # truncate source content before grading
SYNTH_BATCH_DEFAULT = 8    # graded sources per synthesis call (bounded context)
SEARCH_PAGE_DEFAULT = 10   # results to pull per single adapter search

QUERY_PROMPT = """You are a research assistant. Generate {n} distinct web/database \
search queries to investigate this question. Avoid these already-used queries: {past}

Question: {question}

Reply with ONLY a JSON object: {{"queries": ["...", "..."]}}"""

GRADE_PROMPT = """You are grading ONE source for relevance to a research question. \
Use ONLY the source text below — no outside knowledge.

Question: {question}

Source title: {title}
Source text (may be truncated):
{content}

Reply with ONLY a JSON object:
{{"relevant": true/false, "summary": "2-3 sentences", \
"key_claims": ["claim", "..."], "quote": "one short verbatim sentence copied \
exactly from the source text that supports relevance"}}"""

SYNTH_PROMPT = """Write ONE section of a wiki page answering this research \
question, from a single batch of graded findings. The page accumulates many \
such sections over time, so write this as a self-contained contribution — do \
not summarize the whole question, just what THIS batch adds. Follow the schema \
conventions below. Reference sources ONLY as [S1], [S2] etc — never write URLs. \
Use [[wikilinks]] for related concepts. Be factual; only state what the \
findings support.

Question: {question}

Schema conventions:
{schema}

Findings:
{findings}

Reply with ONLY the markdown body (no frontmatter, no title heading)."""

FRONTIER_PROMPT = """A nightly research run just processed these questions with \
these outcomes. Propose frontier updates.

Tonight's work:
{work}

Current open questions:
{open_questions}

Reply with ONLY a JSON object: {{"close": [question numbers that are now \
sufficiently answered], "add": ["new follow-up question", "..."]}}. Be \
conservative about closing; only add questions that tonight's findings raised."""

EXPAND_PROMPT = """You are helping plan a research mission BEFORE it runs. Given \
the mission theme and its current open questions, propose {n} ADDITIONAL \
distinct research questions that broaden and deepen coverage of the theme. Each \
must be specific and answerable, and NOT a duplicate or near-duplicate of an \
existing question.

Mission theme: {theme}

Current open questions:
{open_questions}

Reply with ONLY a JSON object: {{"add": ["question", "..."]}}"""


class Deadline:
    def __init__(self, max_minutes: float):
        self.end = time.monotonic() + max_minutes * 60

    def reached(self) -> bool:
        return time.monotonic() >= self.end


def acquire_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(state_dir / 'researchd.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another researchd run is active; exiting')
    return lock  # keep the handle open for the process lifetime


def _process_question(item, llm, adapters, state, vault, mission, budgets,
                      deadline, schema, run_id, report_lines,
                      work_summary) -> str:
    """Harvest one frontier question by interleaving fetch → grade → append:
    pull a batch of sources, grade each with fresh context, and once enough
    have passed, synthesize a self-contained section and append it to the page.
    Repeat until the deadline hits, sources run dry, or the ceiling is reached.

    This keeps every model call bounded (good for a small local model) and the
    page grows section by section instead of via one giant synthesis. The
    deadline is checked before every fetch and every grade, so a high (or zero =
    unlimited) source ceiling can never run away and starve the rest of the
    night. Returns ingested-source count, or 'skip' if nothing new was written.
    LLM/network errors propagate to run_night's consecutive-failure guard."""
    # Step 1-2: generate queries (LLM); a 0 ceiling means "unlimited, deadline governs"
    past = state.past_queries(mission.slug, item.qid)
    q_json = llm.json(QUERY_PROMPT.format(
        n=budgets['max_queries_per_question'], past=past or 'none',
        question=item.text), step='queries',
        fallback={'queries': [item.text]})
    queries = [q for q in q_json.get('queries', [])
               if isinstance(q, str)][:budgets['max_queries_per_question']]

    ceiling = budgets['max_sources_per_question']            # 0 = unlimited
    synth_batch = budgets.get('synth_batch_size', SYNTH_BATCH_DEFAULT)
    page_size = budgets.get('sources_per_search', SEARCH_PAGE_DEFAULT)
    seen_urls: set = set()
    batch: list = []          # graded findings awaiting synthesis
    ingested = considered = 0
    section_no = 0
    page_ready = False

    def at_ceiling() -> bool:
        return bool(ceiling) and ingested + len(batch) >= ceiling

    def flush() -> None:
        nonlocal batch, section_no, ingested, page_ready
        if not batch:
            return
        section_no += 1
        if not page_ready:
            init_page(vault, item.text, run_id, item.qid, [mission.slug])
            page_ready = True
        findings = '\n\n'.join(
            f'[S{n}] {src.title}\nSummary: {summary}\nClaims: {"; ".join(claims)}'
            for n, (src, summary, claims) in enumerate(batch, start=1))
        body = llm.text(SYNTH_PROMPT.format(
            question=item.text, schema=schema[:2000], findings=findings),
            step='synthesize')
        label = f'{today_str()} · batch {section_no} · {len(batch)} sources'
        append_section(vault, item.qid, item.text, body,
                       [s for s, _, _ in batch], run_id, label)
        append_log(vault, f'section | Q{item.qid} | {label}')
        ingested += len(batch)
        batch = []

    for query in queries:
        if deadline.reached() or at_ceiling():
            break
        state.record_query(mission.slug, item.qid, query, run_id)
        for adapter in adapters:
            if deadline.reached() or at_ceiling():
                break
            try:
                results = adapter.search(query, limit=page_size)
            except Exception as e:
                append_log(vault, f'adapter-error | {adapter.name} | {e}')
                continue
            for src in results:
                if deadline.reached() or at_ceiling():
                    break
                if src.url in seen_urls or state.is_seen(mission.slug, src.url):
                    continue
                seen_urls.add(src.url)
                considered += 1
                # Step 3: grade ONE source, fresh context, so it can't blend.
                # Shape is normalized — a local model may return any fields and
                # one malformed value must not crash the night.
                g = llm.json(GRADE_PROMPT.format(
                    question=item.text, title=src.title,
                    content=src.content[:SOURCE_CHAR_BUDGET]), step='grade',
                    fallback={'relevant': False})
                # Mark every graded source seen — rejected ones too, or they get
                # re-fetched and re-graded every night the question stays open
                state.mark_seen(mission.slug, src.url, run_id)
                quote = g.get('quote')
                quote = quote if isinstance(quote, str) else ''
                summary = str(g.get('summary', '')).strip() or '(no summary)'
                claims = [str(c) for c in (g.get('key_claims') or [])
                          if isinstance(c, (str, int, float))]
                if g.get('relevant') and quote and \
                        quote.lower() in src.content.lower():
                    batch.append((src, summary, claims))
                    save_raw(vault, src, run_id)
                    if len(batch) >= synth_batch:
                        flush()   # Step 4-5: synthesize a section, append it
    flush()  # write the final partial batch

    if ingested == 0:
        report_lines.append(
            f'Q{item.qid}: no new sources found' if considered == 0
            else f'Q{item.qid}: {considered} sources graded, none passed')
        return 'skip'
    report_lines.append(f'Q{item.qid}: wrote {section_no} section(s) '
                        f'from {ingested} sources ({considered} graded)')
    work_summary.append(f'Q{item.qid} ({item.text}): '
                        f'{ingested} sources synthesized')
    return ingested


def expand_frontier(cfg: dict, mission: Mission, count: int) -> list[str]:
    """Pre-planning step (run by hand before a mission): ask the LLM to propose
    `count` new frontier questions and append them for the human to review.
    Reuses the frontier's validate + dated-backup + atomic-write path, so the
    model never edits the file directly. Returns the questions actually added."""
    llm = make_llm(cfg, dry_run=False)
    llm.preflight()
    frontier = mission.frontier()
    open_qs = '\n'.join(f'- {i.text}' for i in frontier.open_items()) \
        or '(none yet)'
    ops = llm.json(EXPAND_PROMPT.format(
        n=count, theme=mission.name, open_questions=open_qs),
        step='frontier', fallback={'add': []})
    add = [a for a in (ops.get('add') or [])
           if isinstance(a, str) and a.strip()]
    applied = frontier.apply_ops({'close': [], 'add': add}, count)
    if applied['added']:
        frontier.save(mission.name)
    added_ids = set(applied['added'])
    return [i.text for i in frontier.items if i.qid in added_ids]


def run_night(cfg: dict, mission: Mission, dry_run: bool,
              max_minutes: float, max_sources: int | None = None) -> tuple:
    run_id = f'{today_str()}-{uuid.uuid4().hex[:8]}'
    lock = acquire_lock(Path(cfg['state_dir']))  # noqa: F841
    # Dry runs are fully sandboxed: separate state DB and wiki dir, and the
    # real frontier.md is never saved — a smoke test must not touch real data
    state = State(cfg['state_db'] if not dry_run
                  else str(Path(cfg['state_dir']) / 'dryrun.db'))
    budgets = mission.budgets(cfg['defaults'])
    if max_sources is not None:  # per-run override of the ceiling (0 = unlimited)
        budgets['max_sources_per_question'] = max_sources
    vault = mission.vault_path if not dry_run else mission.path / 'wiki-dryrun'
    deadline = Deadline(max_minutes)
    report_lines: list[str] = []
    questions_done = sources_ingested = 0
    status = 'ok'

    llm = make_llm(cfg, dry_run)
    try:
        model = llm.preflight()
    except Exception as e:
        write_report(vault, run_id, 'FAILED',
                     [f'pre-flight failed: {e}', 'no research performed'])
        raise SystemExit(f'pre-flight failed: {e}')

    adapter_names = ['mock'] if dry_run else mission.adapters
    try:
        adapters = [get_adapter(name) for name in adapter_names]
    except Exception as e:
        write_report(vault, run_id, 'FAILED',
                     [f'adapter setup failed: {e}', 'no research performed'])
        raise SystemExit(f'adapter setup failed: {e}')
    schema = (mission.path / 'schema.md').read_text(encoding='utf-8') \
        if (mission.path / 'schema.md').exists() else ''
    state.start_run(run_id, mission.slug)
    append_log(vault, f'run-start | {run_id} | model={model} | dry_run={dry_run}')

    frontier = mission.frontier()
    work_summary = []
    llm_failures = 0
    for item in frontier.open_items()[:budgets['max_questions_per_night']]:
        if deadline.reached():
            status = 'deadline'
            report_lines.append('stopped at deadline before finishing frontier')
            break
        try:
            done = _process_question(
                item, llm, adapters, state, vault, mission, budgets,
                deadline, schema, run_id, report_lines, work_summary)
            llm_failures = 0
        except (LLMError, requests.RequestException) as e:
            # One flaky call must not kill the night; two in a row means the
            # server is down — stop cleanly instead of burning the window
            llm_failures += 1
            append_log(vault, f'llm-error | Q{item.qid} | {e}')
            report_lines.append(f'Q{item.qid}: skipped after LLM error')
            if llm_failures >= 2:
                status = 'llm-error'
                report_lines.append('two consecutive LLM failures — stopping')
                break
            continue
        if isinstance(done, int):
            questions_done += 1
            sources_ingested += done
        elif done == 'deadline':
            status = 'deadline'
            report_lines.append(f'Q{item.qid}: stopped at deadline mid-question')
            break

    # Step 5: frontier update (LLM proposes, code validates and writes)
    if work_summary and not deadline.reached():
        try:
            ops = llm.json(FRONTIER_PROMPT.format(
                work='\n'.join(work_summary),
                open_questions='\n'.join(f'Q{i.qid}: {i.text}'
                                         for i in frontier.open_items())),
                step='frontier', fallback={'close': [], 'add': []})
        except (LLMError, requests.RequestException) as e:
            append_log(vault, f'llm-error | frontier | {e}')
            ops = {'close': [], 'add': []}
        applied = frontier.apply_ops(ops, budgets['max_new_frontier_items'])
        if dry_run:
            report_lines.append('frontier: dry-run — proposed ops not saved')
        else:
            frontier.save(mission.name)
            report_lines.append(
                f'frontier: closed {applied["closed"] or "none"}, '
                f'added {applied["added"] or "none"}')

    rebuild_index(vault, mission.name)
    report = write_report(vault, run_id, status,
                          report_lines or ['nothing to do'])
    append_log(vault, f'run-end | {run_id} | {status} | '
                      f'{questions_done} questions, {sources_ingested} sources')
    state.end_run(run_id, status, questions_done, sources_ingested)
    return run_id, report
