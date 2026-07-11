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
from .wiki import append_log, rebuild_index, save_raw, write_page, write_report

SOURCE_CHAR_BUDGET = 6000  # truncate source content before grading

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

SYNTH_PROMPT = """Write the body of a wiki page answering this research question \
from tonight's graded findings. Follow the schema conventions below. Reference \
sources ONLY as [S1], [S2] etc — never write URLs. Use [[wikilinks]] for related \
concepts. Be factual; only state what the findings support.

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
    """Steps 1-4 for one frontier question. Returns the ingested-source count
    on success, or 'skip' / 'deadline'. LLM/network errors propagate."""
    # Step 1-2: generate queries (LLM), search (adapters)
    past = state.past_queries(mission.slug, item.qid)
    q_json = llm.json(QUERY_PROMPT.format(
        n=budgets['max_queries_per_question'], past=past or 'none',
        question=item.text), step='queries',
        fallback={'queries': [item.text]})
    queries = [q for q in q_json.get('queries', [])
               if isinstance(q, str)][:budgets['max_queries_per_question']]

    # Fetch, deduping against prior nights as we go; stop as soon as the
    # per-question source budget is filled (don't pay for discarded results)
    max_sources = budgets['max_sources_per_question']
    fresh, seen_urls = [], set()
    for query in queries:
        if len(fresh) >= max_sources:
            break
        state.record_query(mission.slug, item.qid, query, run_id)
        for adapter in adapters:
            if len(fresh) >= max_sources:
                break
            try:
                results = adapter.search(query, limit=max_sources)
            except Exception as e:
                append_log(vault, f'adapter-error | {adapter.name} | {e}')
                continue
            for c in results:
                if len(fresh) >= max_sources:
                    break
                if c.url not in seen_urls and \
                        not state.is_seen(mission.slug, c.url):
                    fresh.append(c)
                    seen_urls.add(c.url)
    if not fresh:
        report_lines.append(f'Q{item.qid}: no new sources found')
        return 'skip'

    # Step 3: grade each source (one LLM call per source, truncated input).
    # Grade JSON shape is normalized here — a local model can return any
    # shape, and one malformed field must not crash the night.
    graded = []
    for src in fresh:
        if deadline.reached():
            return 'deadline'
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
        if g.get('relevant') and quote and quote.lower() in src.content.lower():
            graded.append((src, summary, claims))
            save_raw(vault, src, run_id)
    if not graded:
        report_lines.append(f'Q{item.qid}: {len(fresh)} sources fetched, '
                            'none passed grading')
        return 'skip'
    if deadline.reached():
        return 'deadline'

    # Step 4: synthesize a wiki page
    findings = '\n\n'.join(
        f'[S{n}] {src.title}\nSummary: {summary}\nClaims: {"; ".join(claims)}'
        for n, (src, summary, claims) in enumerate(graded, start=1))
    body = llm.text(SYNTH_PROMPT.format(
        question=item.text, schema=schema[:2000], findings=findings),
        step='synthesize')
    page = write_page(vault, title=item.text, body=body,
                      sources=[src for src, _, _ in graded], run_id=run_id,
                      question_id=item.qid, tags=[mission.slug])
    append_log(vault, f'page | Q{item.qid} | {page.name} | '
                      f'{len(graded)} sources')
    report_lines.append(f'Q{item.qid}: wrote {page.name} '
                        f'from {len(graded)} sources')
    work_summary.append(f'Q{item.qid} ({item.text}): '
                        f'{len(graded)} sources synthesized')
    return len(graded)


def run_night(cfg: dict, mission: Mission, dry_run: bool,
              max_minutes: float) -> tuple:
    run_id = f'{today_str()}-{uuid.uuid4().hex[:8]}'
    lock = acquire_lock(Path(cfg['state_dir']))  # noqa: F841
    # Dry runs are fully sandboxed: separate state DB and wiki dir, and the
    # real frontier.md is never saved — a smoke test must not touch real data
    state = State(cfg['state_db'] if not dry_run
                  else str(Path(cfg['state_dir']) / 'dryrun.db'))
    budgets = mission.budgets(cfg['defaults'])
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
