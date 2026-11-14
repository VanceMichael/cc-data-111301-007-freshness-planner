"""固定虚拟时钟下的一周重访演练。

来源行为完全由脚本决定（含真实改版时刻，仅用于事后评估，计划器看不到）。
计划器只能通过抓取结果认识世界，因此可以看清：

- 每次为什么选择这个地址（打分分量与理由随决策一起输出）；
- 哪些新鲜度目标因为预算不足而没有资源满足；
- 变化发现延迟（真实改版时刻 -> 首次抓到变化），而不只是命中次数；
- 连续失败退避、新地址探索、重定向身份迁移的实际效果。
"""

from dataclasses import dataclass, field

from .observations import Observation
from .planner import Planner


@dataclass
class ScriptedSource:
    """演练来源：versions 按天给出正文版本，failures 给出失败日期。"""
    versions: dict                      # url -> [{'day': int, 'digest': ..., 'etag': ...}]
    failures: dict = field(default_factory=dict)   # url -> set[int]
    redirects: dict = field(default_factory=dict)  # url -> {'day': int, 'target': ..., 'permanent': bool}

    def active_version(self, url, day):
        timeline = self.versions.get(url)
        if not timeline:
            return None
        active = None
        for event in timeline:
            if event['day'] <= day:
                active = event
        return active

    def fetch(self, url, day, conditional_etag):
        scheduled = self.redirects.get(url)
        if scheduled and scheduled['day'] <= day:
            if not scheduled.get('consumed_migration'):
                scheduled['consumed_migration'] = True
                return {'kind': 'redirect',
                        'status': 301 if scheduled['permanent'] else 302,
                        'target': scheduled['target'],
                        'permanent': scheduled['permanent']}
            # 迁移完成后旧地址按退休处理，不再返回旧内容
            return {'kind': 'failure', 'status': 404}
        if day in self.failures.get(url, set()):
            return {'kind': 'failure', 'status': 503}
        active = self.active_version(url, day)
        if active is None:
            return {'kind': 'failure', 'status': 404}
        if conditional_etag is not None and conditional_etag == active['etag']:
            # 304 的证据范围是"该 etag 对应内容仍有效"，与正文摘要比较不同：
            # 它由源站条件请求直接确认，不依赖本地摘要实现。
            return {'kind': '304', 'status': 304,
                    'etag': active['etag'], 'digest': active['digest']}
        return {'kind': '200', 'status': 200,
                'etag': active['etag'], 'digest': active['digest']}

    def change_events(self, url):
        return list(self.versions.get(url, ()))[1:]


def build_week_scenario():
    """一周演练剧本（虚拟天 0..6，每日预算 2）。"""
    pages = [
        {'url': '/activity', 'importance': 10, 'max_staleness': 2},
        {'url': '/manual', 'importance': 2, 'max_staleness': 30},
        {'url': '/broken', 'importance': 5, 'max_staleness': 3},
        {'url': '/new', 'importance': 3, 'max_staleness': 4},
        {'url': '/old-doc', 'importance': 4, 'max_staleness': 3,
         'last_visit': 0},
        {'url': '/quiet', 'importance': 1, 'max_staleness': 14,
         'last_visit': 0},
    ]
    versions = {
        '/activity': [
            {'day': 0, 'digest': 'act-v1', 'etag': 'W/"a1"'},
            {'day': 3, 'digest': 'act-v2', 'etag': 'W/"a2"'},
        ],
        '/manual': [
            {'day': 0, 'digest': 'manual-v1', 'etag': '"m1"'},
        ],
        '/broken': [
            {'day': 0, 'digest': 'broken-v1', 'etag': '"b1"'},
        ],
        '/new': [
            {'day': 0, 'digest': 'new-v1', 'etag': '"n1"'},
            # 与 /manual 正文相同但 etag 不同：摘要相同也不允许合并历史
            {'day': 5, 'digest': 'manual-v1', 'etag': '"n2"'},
        ],
        '/old-doc': [
            {'day': 0, 'digest': 'old-v1', 'etag': '"o1"'},
        ],
        '/docs-new': [
            {'day': 2, 'digest': 'old-v1', 'etag': '"o1"'},
        ],
        '/quiet': [
            {'day': 0, 'digest': 'quiet-v1', 'etag': '"q1"'},
        ],
    }
    failures = {'/broken': {0, 1, 2, 3, 4, 5, 6}}
    redirects = {
        '/old-doc': {'day': 2, 'target': '/docs-new', 'permanent': True},
    }
    source = ScriptedSource(versions, failures, redirects)
    return pages, source, 2, 7


def _etag_of(state):
    for item in reversed(state.observations):
        if item.kind in ('304', 'unchanged', 'changed') and item.note:
            return item.note.get('etag')
    return None


def _digest_of(state):
    for item in reversed(state.observations):
        if item.kind in ('unchanged', 'changed'):
            return item.digest
    return None


def run_week(pages=None, source=None, daily_budget=None, horizon=7,
             start_day=0):
    if pages is None:
        pages, source, daily_budget, horizon = build_week_scenario()
    planner = Planner(pages, daily_budget, today=start_day)
    days = []
    for day in range(start_day, start_day + horizon):
        planner.today = day
        ranking = planner.plan(day)
        visits, skipped = [], []
        while planner.remaining_budget(day) > 0:
            candidates = [item for item in planner.plan(day) if item['eligible']]
            if not candidates:
                skipped.append('没有满足条件的候选地址（退避中或均已退休）')
                break
            choice = candidates[0]
            token = planner.issue(choice['url'], day)
            state = planner.pages[choice['url']]
            result = source.fetch(
                choice['url'], day, _etag_of(state))
            visit = {'token': token.token_id, 'url': choice['url'],
                     'priority': choice['priority'],
                     'components': choice['components'],
                     'reasons': choice['reasons'], 'outcome': None}
            if result['kind'] == 'redirect':
                observation = Observation(
                    day, 'redirect', status=result['status'],
                    target=result['target'], permanent=result['permanent'])
                planner.record(token, observation)
                migrated = planner.migrate_identity(
                    choice['url'], result['target'], day,
                    permanent=result['permanent'])
                visit['outcome'] = (
                    f"重定向到 {result['target']}（"
                    + ('永久，历史已继承' if migrated is not None
                       else '临时，不迁移身份') + '）')
            elif result['kind'] == 'failure':
                planner.record(token,
                               Observation(day, 'failure',
                                           status=result['status']))
                visit['outcome'] = f"抓取失败 status={result['status']}，不更新内容时间"
            else:
                previous = _digest_of(state)
                if result['kind'] == '304':
                    kind = '304'
                    outcome = '304 条件确认，内容未变'
                elif previous is None:
                    kind = 'unchanged'
                    outcome = '首次取得正文，建立基线'
                elif previous == result['digest']:
                    kind = 'unchanged'
                    outcome = '200 且正文摘要与上次相同'
                else:
                    kind = 'changed'
                    outcome = f"正文变化 {previous} -> {result['digest']}（真实改版时刻仅落在区间内）"
                planner.record(token, Observation(
                    day, kind, digest=result['digest'],
                    status=result['status'],
                    note={'etag': result['etag']}))
                visit['outcome'] = outcome
            visits.append(visit)
        if planner.remaining_budget(day) > 0 and not skipped:
            skipped.append(f"剩余 {planner.remaining_budget(day)} 次预算无地址可用")
        days.append({
            'day': day,
            'remaining_budget': planner.remaining_budget(day),
            'ranking': [
                {'url': item['url'], 'eligible': item['eligible'],
                 'priority': item['priority'],
                 'components': item['components'],
                 'reasons': item['reasons']}
                for item in ranking],
            'visits': visits,
            'skipped': skipped,
            'unmet_targets': planner.unmet_targets(day),
        })
    evaluation = _evaluate(planner, source, start_day + horizon - 1)
    return {'planner': planner, 'days': days, 'evaluation': evaluation}


def _evaluate(planner, source, last_day):
    """用来源真实改版时刻评估新鲜度与发现延迟（计划器看不到这些数据）。"""
    per_page, detection_lags = [], []
    for url, state in planner.pages.items():
        entry = {'url': url, 'retired': state.retired,
                 'importance': state.importance,
                 'last_success': state.last_success,
                 'consecutive_failures': state.consecutive_failures}
        if state.retired:
            entry['status'] = f"已退休，身份迁移至 {state.alias_of}"
            per_page.append(entry)
            continue
        active = source.active_version(url, last_day)
        if active is None:
            entry['status'] = '来源已下线'
            per_page.append(entry)
            continue
        change_day = active['day']
        if state.last_success is None:
            entry['status'] = '整周未取得内容'
            entry['stale_days'] = None
        elif state.last_success < change_day:
            entry['stale_days'] = last_day - change_day + 1
            entry['status'] = f"第 {change_day} 天改版，至周末仍未抓到，已陈旧"
        else:
            entry['stale_days'] = 0
            entry['status'] = '周末持有的副本为最新版本'
        for event in source.change_events(url):
            first_seen = next(
                (item.time for item in state.observations
                 if item.kind == 'changed' and item.time >= event['day']),
                None)
            if first_seen is not None:
                detection_lags.append({
                    'url': url, 'changed_on': event['day'],
                    'detected_on': first_seen,
                    'lag_days': first_seen - event['day']})
        per_page.append(entry)
    return {'as_of_day': last_day, 'pages': per_page,
            'detection_lags': detection_lags,
            'unmet_targets': planner.unmet_targets(last_day)}
