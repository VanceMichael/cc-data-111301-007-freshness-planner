import json
from pathlib import Path


def fixed_cycle(pages, now):
    if now < 0:
        raise ValueError('时间不可为负数')
    return [page['url'] for page in pages if page['last_visit'] is None or page['last_visit'] + page['interval'] <= now]


def inspect_file(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    return {'now':value['now'],'fixed_due':fixed_cycle(value['pages'], value['now']), 'budget':value['daily_budget']}
