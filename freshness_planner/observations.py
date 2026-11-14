"""观测证据与变化率估计。

证据分类（语义不同，不能互相替代）：

- ``304``      条件请求得到确认，强证据：区间内该 URL 内容未变
- ``unchanged`` 200 且正文摘要与上次相同，证据范围只覆盖抓取成功的时刻
- ``changed``  200 且正文摘要变化，只能说明变化落在上一次成功抓取与本次之间
- ``failure``  短暂失败（5xx/网络错误），不携带内容是否变化的信息
- ``redirect`` 显式永久重定向，是身份迁移的唯一合法依据
- ``legacy``   旧格式资料里只有 last_visit、没有摘要的历史访问

源页面的真实修改时刻通常无法确定，只能确定落在两次成功抓取构成的删失区间
内。因此本模块从不把"抓到变化的时间"当作源修改时间，而是对区间建模：
假设页面变化服从泊松过程，变化率 lambda 由区间证据做极大似然估计，
并叠加一个弱 Gamma 先验解决无历史冷启动。
"""

import math

SUCCESS_KINDS = ('304', 'unchanged', 'changed', 'legacy')
CONTENT_KINDS = ('304', 'unchanged', 'changed')
VALID_KINDS = SUCCESS_KINDS + ('failure', 'redirect')

# 弱先验：Gamma(alpha=1, beta=30)，先验均值约每 30 天一次变化。
# 先验只在缺乏证据时起作用，观测暴露时间越长影响越小。
PRIOR_ALPHA = 1.0
PRIOR_BETA = 30.0
RATE_FLOOR = 1.0 / 3650.0  # 长期无变化时的估计下限，避免数值归零


class Observation:
    """一次抓取留下的证据。"""

    __slots__ = ('time', 'kind', 'digest', 'status', 'target',
                 'permanent', 'origin', 'note')

    def __init__(self, time, kind, *, digest=None, status=None,
                 target=None, permanent=True, origin=None, note=None):
        if kind not in VALID_KINDS:
            raise ValueError(f'未知观测类型: {kind}')
        if time is not None and time < 0:
            raise ValueError('观测时间不可为负数')
        if kind == 'redirect' and not target:
            raise ValueError('重定向观测必须携带目标地址')
        self.time = time
        self.kind = kind
        self.digest = digest
        self.status = status
        self.target = target
        self.permanent = permanent
        # origin 非空表示这条证据是身份迁移时从旧地址继承来的
        self.origin = origin
        self.note = note

    def to_dict(self):
        return {key: getattr(self, key) for key in self.__slots__
                if getattr(self, key) is not None}


def build_intervals(observations):
    """把按时序排列的观测转成删失区间证据。

    返回 ``(区间长度, 是否观测到变化)`` 列表：

    - 相邻两次成功抓取，后一次为 changed -> 区间内至少变化一次
    - 后一次为 304/unchanged -> 区间内零变化
    - 任一端是 legacy（无摘要）或 failure -> 没有内容证据，跳过
    """
    successes = sorted(
        (item for item in observations if item.kind in SUCCESS_KINDS),
        key=lambda item: item.time)
    # 区间内部夹着失败或无摘要的 legacy 访问时，该区间的内容状态无法闭合，
    # 不能当作"无变化"或"有变化"的证据。
    gaps = {item.time for item in observations
            if item.kind in ('failure', 'legacy')}
    intervals = []
    for previous, current in zip(successes, successes[1:]):
        if previous.kind == 'legacy' or current.kind == 'legacy':
            continue
        span = current.time - previous.time
        if span <= 0:
            continue
        if any(previous.time < gap < current.time for gap in gaps):
            continue
        intervals.append((span, current.kind == 'changed'))
    return intervals


def _derivative(rate, changed_spans, same_spans):
    """对数似然（含 Gamma 先验）对 lambda 的一阶导，随 lambda 单调递减。"""
    value = (PRIOR_ALPHA - 1.0) / rate - PRIOR_BETA
    value += sum(span / math.expm1(rate * span) for span in changed_spans)
    value -= sum(same_spans)
    return value


def estimate_rate(observations):
    """估计页面变化率 lambda（单位：次/天），返回可解释的诊断信息。"""
    intervals = build_intervals(observations)
    changed_spans = [span for span, changed in intervals if changed]
    same_spans = [span for span, changed in intervals if not changed]
    exposure = sum(changed_spans) + sum(same_spans)

    if not changed_spans:
        # 没有任何变化证据时，MLE 在 lambda=0，使用先验/下限并说明原因
        rate = RATE_FLOOR if same_spans else PRIOR_ALPHA / PRIOR_BETA
        return {
            'rate': rate,
            'exposure_days': exposure,
            'changed_intervals': 0,
            'unchanged_intervals': len(same_spans),
            'basis': 'prior_only' if not same_spans else 'no_change_observed',
        }

    low, high = 1e-9, 1.0
    while _derivative(high, changed_spans, same_spans) >= 0:
        high *= 2
        if high > 1e6:
            break
    for _ in range(60):
        middle = (low + high) / 2
        if _derivative(middle, changed_spans, same_spans) > 0:
            low = middle
        else:
            high = middle
    rate = max((low + high) / 2, RATE_FLOOR)
    return {
        'rate': rate,
        'exposure_days': exposure,
        'changed_intervals': len(changed_spans),
        'unchanged_intervals': len(same_spans),
        'basis': 'maximum_likelihood',
    }


def probability_changed(rate, elapsed):
    """泊松过程下，经过 elapsed 时间至少发生一次变化的概率。"""
    if elapsed <= 0:
        return 0.0
    return -math.expm1(-rate * elapsed)
