DESCRIPTION = '有限访问预算下的网页重访与新鲜度控制'
SAMPLE = 'fixtures/pages.json'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .planner import Planner
from .simulation import run_week


def plan_file(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    planner = Planner(value['pages'], value['daily_budget'],
                      today=value['now'])
    return {
        'now': value['now'],
        'daily_budget': value['daily_budget'],
        'remaining_budget': planner.remaining_budget(value['now']),
        'ranking': planner.plan(value['now']),
        'unmet_targets': planner.unmet_targets(value['now']),
    }


def rehearse():
    result = run_week()
    return {
        'daily_budget': result['planner'].daily_budget,
        'days': result['days'],
        'evaluation': result['evaluation'],
    }


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command',
                        choices=['inspect', 'capture', 'demo', 'plan', 'rehearse'])
    parser.add_argument('path', nargs='?', default=SAMPLE)
    parser.add_argument('--archive', default='.work/raw')
    args = parser.parse_args()
    if args.command == 'inspect':
        result = inspect_file(Path(args.path))
    elif args.command == 'capture':
        result = archive_file(Path(args.path), Path(args.archive))
    elif args.command == 'plan':
        result = plan_file(Path(args.path))
    elif args.command == 'rehearse':
        result = rehearse()
    else:
        with tempfile.TemporaryDirectory() as directory:
            result = {'原始资料': archive_file(Path(args.path), Path(directory)),
                      '资料预览': inspect_file(Path(args.path))}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
