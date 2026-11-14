"""自适应重访计划。

策略是可解释的加权打分，不依赖预测模型：每个候选地址的优先级由业务重要度、
截止压力、变化风险、探索机会、饥饿惩罚五个含义明确的分量相加组成，饥饿打满
时再叠加独立的防饿死强保底；预算不足时按优先级截断，未被满足的新鲜度目标
由 :meth:`Planner.unmet_targets` 明确报告。

设计要点：

1. 变化认知来自 :mod:`freshness_planner.observations` 的删失区间估计，
   抓取到变化的时间不会被当成源修改时间。
2. 预算在"签发请求"时记账，已经发出的请求不会因重新计划而被算两遍；
   同一地址在已有请求未返回时不会被重复签发。
3. 连续失败采用指数退避，但等待退避会突破业务截止时间时可以提前重试。
4. 新地址有探索分量，冷门地址有与重要度无关的防饿死保底，
   在有界窗口内必然获得一次预算，不会永久饿死。
5. 历史只能通过显式永久重定向迁移；正文摘要相同绝不合并两条历史。
"""

from dataclasses import dataclass, field
from itertools import count

from .observations import (
    Observation, estimate_rate, probability_changed,
)

# 打分权重（可解释，不是训练出来的参数）
# 优先级为加法模型：重要度、截止压力、风险、探索、饥饿分开相加，
# 饥饿打满后再叠加独立的防饿死强保底（见 STARVATION_GUARANTEE）。
W_IMPORTANCE = 1.0     # 业务重要度（参与排序时按 IMPORTANCE_CAP 封顶）
W_URGENCY = 2.0        # 业务截止时间压力
W_RISK = 1.5           # 内容已经发生变化的概率
W_EXPLORE = 1.0        # 无内容证据地址的探索机会
W_STARVATION = 3.0     # 长期未访问的常规饥饿惩罚（线性累积，影响常态排序）
# 防饿死强保底：饥饿打满后叠加的独立分量，取值高于"封顶重要度 + 常规分量"
# 的上限（10 + 2 + 1.5 + 1 = 14.5）。因此任何地址在自身可接受陈旧时长的
# 两倍窗口内必然赢得一次预算；窗口未满前该分量为 0，不干扰常态排序。
STARVATION_GUARANTEE = 16.0
# 重要度参与排序时封顶：否则任意大的重要度都会让冷门地址永久饿死。
# 封顶不改变常规排序（差距在 10 倍以内时），只限制极端差距。
IMPORTANCE_CAP = 10.0
# 截止压力封顶：逾期再久，紧迫性分量也不超过 2，避免单一逾期页面
# 永久霸占全部预算，把冷门地址彻底饿死。
URGENCY_CAP = 2.0
MAX_BACKOFF = 4       # 失败退避上限（虚拟天）
TOKEN_PREFIX = 'visit'


@dataclass
class VisitToken:
    """一次已签发但尚未记账结果的访问。"""
    token_id: str
    url: str
    day: int
    recorded: bool = False


@dataclass
class PageState:
    url: str
    importance: float
    max_staleness: int
    first_seen: int
    observations: list = field(default_factory=list)
    last_attempt: int | None = None
    last_success: int | None = None
    last_visit_day: int | None = None
    consecutive_failures: int = 0
    rate_info: dict | None = None
    retired: bool = False
    alias_of: str | None = None
    redirect_chain: tuple = ()

    @property
    def has_content_evidence(self):
        return any(item.kind in ('304', 'unchanged', 'changed')
                   for item in self.observations)

    @property
    def pending(self):
        return getattr(self, '_pending', False)

    def deadline(self, now):
        """业务新鲜度截止时间；从未取得内容时截止时间就是当前时刻。"""
        if self.last_success is None:
            return now
        return self.last_success + self.max_staleness

    def backoff_delay(self):
        if self.consecutive_failures <= 0:
            return 0
        return min(2 ** (self.consecutive_failures - 1), MAX_BACKOFF)


class Planner:
    def __init__(self, pages, daily_budget, *, today=0):
        if daily_budget < 0:
            raise ValueError('每日预算不可为负数')
        self.daily_budget = daily_budget
        self.today = today
        self._remaining = {}       # day -> 当日剩余预算
        self._tokens = {}          # token_id -> VisitToken
        self._counter = count(1)
        self.pages = {}
        for page in pages:
            self.pages[page['url']] = self._build_page(page, today)

    # ------------------------------------------------------------------ 构建
    def _build_page(self, page, now):
        state = PageState(
            url=page['url'],
            importance=float(page['importance']),
            max_staleness=int(page['max_staleness']),
            first_seen=int(page.get('first_seen', now)),
        )
        # 兼容旧格式：只有 last_visit、没有摘要的访问记为 legacy，
        # 它能说明"曾经访问过"，但不进入变化率的区间证据。
        if page.get('last_visit') is not None:
            visited_at = int(page['last_visit'])
            state.observations.append(Observation(visited_at, 'legacy'))
            state.last_attempt = visited_at
        for raw in page.get('observations', []):
            state.observations.append(Observation(**raw))
        self._refresh(state)
        return state

    def _refresh(self, state):
        state.rate_info = estimate_rate(state.observations)
        successes = [item.time for item in state.observations
                     if item.kind in ('304', 'unchanged', 'changed')]
        if successes:
            state.last_success = max(successes)
        state.last_attempt = max(
            (item.time for item in state.observations), default=None)
        state.consecutive_failures = 0
        for item in sorted(state.observations, key=lambda x: x.time,
                           reverse=True):
            if item.kind == 'failure':
                state.consecutive_failures += 1
            elif item.kind in ('304', 'unchanged', 'changed'):
                break

    # ------------------------------------------------------------------ 预算
    def remaining_budget(self, day=None):
        day = self.today if day is None else day
        return self._remaining.setdefault(day, self.daily_budget)

    def plan(self, now=None):
        """返回当前时刻全部可选地址的排序与逐条打分理由（不花钱）。"""
        now = self.today if now is None else now
        if now < 0:
            raise ValueError('时间不可为负数')
        ranking = []
        for state in self.pages.values():
            entry = self._score(state, now)
            if entry['eligible']:
                ranking.append(entry)
        ranking.sort(key=lambda item: item['priority'], reverse=True)
        return ranking

    def _score(self, state, now):
        components, notes = {}, []
        if state.retired:
            return self._ineligible(state, now, '地址已退休', components)
        if state.pending:
            return self._ineligible(state, now, '已有访问未返回，避免重复计数',
                                    components)

        deadline = state.deadline(now)
        headroom = deadline - now
        if state.last_success is None:
            urgency = 1.0
            notes.append('从未取得过内容，截止时间按当前时刻计')
        elif headroom <= 0:
            urgency = min(
                1.0 + (-headroom) / state.max_staleness, URGENCY_CAP)
            notes.append(f'已超出新鲜度截止时间 {-headroom} 天')
        else:
            urgency = 1.0 - headroom / state.max_staleness
            notes.append(f'距截止时间还有 {headroom} 天')
        components['urgency'] = round(urgency, 4)

        if state.last_success is None:
            risk = 1.0
        else:
            risk = probability_changed(
                state.rate_info['rate'], now - state.last_success)
        components['change_risk'] = round(risk, 4)
        notes.append(
            f"变化率 {state.rate_info['rate']:.4g} 次/天"
            f"（依据：{state.rate_info['basis']}，"
            f"暴露 {state.rate_info['exposure_days']:.1f} 天）")

        explore = 1.0 if not state.has_content_evidence else 0.0
        components['explore'] = explore
        if explore:
            notes.append('缺少内容证据，给予探索权重')

        # 饥饿锚点是"上次取得内容"而非"上次尝试"：连续失败不会缓解饥饿，
        # 冷门地址也不会因为被失败访问过就失去防饿死保底。
        # 从未取得内容的页面从 first_seen 起算，饥饿在窗口内线性爬升，
        # 而不是一进入计划就打满——否则新地址会立刻挤掉在保的重要页面。
        if state.last_success is not None:
            anchor = state.last_success
        else:
            anchor = state.first_seen
        starvation = min(
            1.0, max(0.0, (now - anchor)) / (2 * state.max_staleness))
        components['starvation'] = round(starvation, 4)
        if starvation >= 1.0:
            notes.append(
                f'自第 {anchor} 天起 {now - anchor} 天未取得内容，'
                '饥饿已打满')

        # 加法模型：重要度决定常态排序，饥饿保底保证冷门地址不会永久饿死
        starvation_full = starvation >= 1.0
        guarantee = STARVATION_GUARANTEE if starvation_full else 0.0
        priority = (W_IMPORTANCE * min(state.importance, IMPORTANCE_CAP)
                    + W_URGENCY * urgency
                    + W_RISK * risk
                    + W_EXPLORE * explore
                    + W_STARVATION * starvation
                    + guarantee)
        if starvation_full:
            notes.append('饥饿已打满，防饿死强保底生效：'
                         f'在 {2 * state.max_staleness} 天窗口内保证获得预算')

        backoff = state.backoff_delay()
        eligible = True
        bypass_daily_limit = False
        if backoff and state.last_attempt is not None:
            next_allowed = state.last_attempt + backoff
            if now < next_allowed:
                if state.last_success is not None and next_allowed >= deadline:
                    # 只有"曾取得内容、等到退避结束会突破业务截止时间"才提前重试；
                    # 从未成功过的地址仍按退避节奏探索，避免失败刷屏挤占预算
                    bypass_daily_limit = True
                    notes.append(f'连续失败 {state.consecutive_failures} 次，'
                                 f'退避本应至第 {next_allowed} 天，'
                                 '但会突破业务截止时间，允许提前重试')
                else:
                    eligible = False
                    notes.append(f'连续失败 {state.consecutive_failures} 次，'
                                 f'退避至第 {next_allowed} 天')
        if eligible and state.last_visit_day == now and not bypass_daily_limit:
            notes.append('同一地址每个虚拟日最多访问一次')
            eligible = False
        components = {
            **components,
            'importance': state.importance,
            'importance_used': round(
                min(state.importance, IMPORTANCE_CAP), 4),
            'starvation_guarantee': guarantee,
            'priority': round(priority, 4),
        }
        if state.importance > IMPORTANCE_CAP:
            notes.append(f'重要度按 {IMPORTANCE_CAP:g} 封顶参与排序，'
                         '防止冷门地址永久饿死')
        return {'url': state.url, 'eligible': eligible,
                'priority': round(priority, 4) if eligible else 0.0,
                'components': components, 'reasons': notes}

    def _ineligible(self, state, now, reason, components):
        return {'url': state.url, 'eligible': False, 'priority': 0.0,
                'components': {**components, 'importance': state.importance},
                'reasons': [reason]}

    # -------------------------------------------------------------- 签发/记账
    def issue(self, url, now=None):
        """签发一次访问并立即扣减当日预算；重排计划不会重复扣账。"""
        now = self.today if now is None else now
        state = self.pages.get(url)
        if state is None:
            raise KeyError(f'未知地址: {url}')
        entry = self._score(state, now)
        if not entry['eligible']:
            raise PermissionError(f'{url} 当前不满足访问条件: {entry["reasons"]}')
        if self.remaining_budget(now) <= 0:
            raise PermissionError(f'第 {now} 天预算已用完')
        token = VisitToken(f'{TOKEN_PREFIX}-{next(self._counter)}', url, now)
        self._tokens[token.token_id] = token
        self._remaining[now] -= 1
        state._pending = True
        return token

    def issue_top(self, now=None):
        """按当前排序签发一个最高优先级地址，预算耗尽返回 None。"""
        now = self.today if now is None else now
        if self.remaining_budget(now) <= 0:
            return None
        ranking = self.plan(now)
        if not ranking:
            return None
        return self.issue(ranking[0]['url'], now)

    def record(self, token, observation):
        """登记一次抓取结果，失败不抹掉内容证据，成功才刷新内容时间。"""
        if isinstance(token, str):
            token = self._tokens.get(token)
        if token is None or token not in self._tokens.values():
            raise KeyError('访问凭证无效或不属于本计划器')
        if token.recorded:
            raise PermissionError('该访问已经记账，不能重复计算')
        state = self.pages[token.url]
        if observation.time is None:
            observation.time = token.day
        if observation.time < token.day:
            raise ValueError('观测时间早于访问签发时间')
        state.observations.append(observation)
        token.recorded = True
        state._pending = False
        state.last_visit_day = token.day
        self._refresh(state)
        return state

    def release(self, token):
        """访问最终没有发出（如客户端放弃）时归还预算。"""
        if isinstance(token, str):
            token = self._tokens.get(token)
        if token is None or token.recorded:
            return
        state = self.pages[token.url]
        state._pending = False
        state.last_visit_day = None
        self._remaining[token.day] += 1
        token.recorded = True

    # -------------------------------------------------------------- 身份迁移
    def migrate_identity(self, source_url, target_url, time, *,
                         permanent=True):
        """按重定向证据迁移页面身份。

        只有显式（默认永久）重定向可以继承历史；临时重定向只记账不迁移。
        正文摘要相同不构成迁移理由，本方法是历史继承的唯一入口。
        """
        source = self.pages.get(source_url)
        if source is None:
            raise KeyError(f'未知地址: {source_url}')
        redirect = Observation(time, 'redirect', target=target_url,
                               permanent=permanent)
        source.observations.append(redirect)
        if not permanent:
            self._refresh(source)
            return None

        inherited = [
            Observation(item.time, item.kind, digest=item.digest,
                        status=item.status, target=item.target,
                        permanent=item.permanent,
                        origin=item.origin or source_url, note=item.note)
            for item in source.observations if item.kind != 'redirect'
        ]
        source.retired = True
        source.alias_of = target_url
        source.redirect_chain = source.redirect_chain + (target_url,)
        source._pending = False

        target = self.pages.get(target_url)
        if target is None:
            target = PageState(
                url=target_url, importance=source.importance,
                max_staleness=source.max_staleness, first_seen=source.first_seen,
                observations=inherited,
                redirect_chain=source.redirect_chain)
            self.pages[target_url] = target
        else:
            target.observations.extend(inherited)
            target.observations.sort(key=lambda item: item.time)
        # 重定向是当天这次访问的结果，新身份当日不再重复签发
        target.last_visit_day = source.last_visit_day
        self._refresh(target)
        return target

    # -------------------------------------------------------------- 目标审计
    def unmet_targets(self, now=None):
        """报告当前没有资源/尚未满足的新鲜度目标，按业务影响排序。"""
        now = self.today if now is None else now
        report = []
        for state in self.pages.values():
            if state.retired:
                continue
            deadline = state.deadline(now)
            if state.last_success is None:
                report.append({
                    'url': state.url, 'importance': state.importance,
                    'overdue_days': None,
                    'reason': '从未取得内容，新鲜度目标无法证明满足',
                })
            elif deadline < now:
                report.append({
                    'url': state.url, 'importance': state.importance,
                    'overdue_days': now - deadline,
                    'reason': f'持有的副本已超出可接受陈旧时长 '
                              f'{now - deadline} 天',
                })
        report.sort(
            key=lambda item: (item['overdue_days'] is None,
                              -(item['overdue_days'] or 0) * item['importance']),
        )
        return report
