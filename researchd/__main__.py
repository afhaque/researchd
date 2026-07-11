"""CLI entrypoint: python -m researchd <command>."""

import argparse
import sys

from .config import load_config
from .missions import (Mission, create_mission, get_active, list_missions,
                       missions_root, set_active)
from .pipeline import expand_frontier, run_night


def cmd_mission_new(cfg, args):
    questions = args.question or []
    m = create_mission(cfg, args.name, args.adapters.split(','), questions)
    set_active(cfg, m.slug)
    print(f'created mission {m.slug!r} (now active)')
    print(f'  edit {m.path / "frontier.md"} to seed research questions')
    print(f'  edit {m.path / "mission.yaml"} to point vault_path at your vault')


def cmd_mission_list(cfg, args):
    active = get_active(cfg)
    missions = list_missions(cfg)
    if not missions:
        print('no missions yet — try: python -m researchd mission new "Name"')
        return
    for m in missions:
        marker = '*' if m.slug == active else ' '
        open_count = len(m.frontier().open_items())
        print(f'{marker} {m.slug}  ({open_count} open questions, '
              f'adapters: {",".join(m.adapters)})')


def cmd_mission_use(cfg, args):
    set_active(cfg, args.slug)
    print(f'active mission: {args.slug}')


def cmd_run(cfg, args):
    slug = args.mission or get_active(cfg)
    if not slug:
        sys.exit('no active mission; use: python -m researchd mission use <slug>')
    mission = Mission(missions_root(cfg) / slug)
    run_id, report = run_night(cfg, mission, dry_run=args.dry_run,
                               max_minutes=args.max_minutes,
                               max_sources=args.max_sources)
    print(f'run {run_id} complete — see {report}')


def cmd_frontier_expand(cfg, args):
    slug = args.mission or get_active(cfg)
    if not slug:
        sys.exit('no active mission; use: python -m researchd mission use <slug>')
    mission = Mission(missions_root(cfg) / slug)
    added = expand_frontier(cfg, mission, args.count)
    if not added:
        print('no new questions proposed')
        return
    print(f'added {len(added)} question(s) to {mission.path / "frontier.md"} '
          '— review and prune before running:')
    for text in added:
        print(f'  + {text}')


def main():
    parser = argparse.ArgumentParser(prog='researchd')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_mission = sub.add_parser('mission', help='manage research missions')
    msub = p_mission.add_subparsers(dest='mcmd', required=True)
    p_new = msub.add_parser('new', help='create a mission')
    p_new.add_argument('name')
    p_new.add_argument('--adapters', default='mock',
                       help='comma-separated: mock,tavily,pubmed')
    p_new.add_argument('--question', action='append',
                       help='seed frontier question (repeatable)')
    msub.add_parser('list', help='list missions')
    p_use = msub.add_parser('use', help='set active mission')
    p_use.add_argument('slug')

    p_run = sub.add_parser('run', help='run a nightly research cycle')
    p_run.add_argument('--mission', help='mission slug (default: active)')
    p_run.add_argument('--dry-run', action='store_true',
                       help='mock LLM + mock search; no GPU or keys needed')
    p_run.add_argument('--max-minutes', type=float, default=360,
                       help='wall-clock budget (default 360)')
    p_run.add_argument('--max-sources', type=int, default=None,
                       help='override per-question source ceiling for this run '
                            '(0 = unlimited; deadline governs)')

    p_frontier = sub.add_parser('frontier', help='pre-plan the frontier')
    fsub = p_frontier.add_subparsers(dest='fcmd', required=True)
    p_expand = fsub.add_parser(
        'expand', help='LLM-propose N new questions and append them for review')
    p_expand.add_argument('--count', type=int, default=10,
                          help='how many new questions to propose (default 10)')
    p_expand.add_argument('--mission', help='mission slug (default: active)')

    args = parser.parse_args()
    cfg = load_config()
    if args.cmd == 'mission':
        {'new': cmd_mission_new, 'list': cmd_mission_list,
         'use': cmd_mission_use}[args.mcmd](cfg, args)
    elif args.cmd == 'run':
        cmd_run(cfg, args)
    elif args.cmd == 'frontier':
        {'expand': cmd_frontier_expand}[args.fcmd](cfg, args)


if __name__ == '__main__':
    main()
