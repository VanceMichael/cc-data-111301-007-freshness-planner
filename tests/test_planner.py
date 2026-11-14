import unittest

from freshness_planner.observations import Observation
from freshness_planner.planner import Planner, STARVATION_GUARANTEE


def make_planner(pages, budget=2, today=0):
    return Planner(pages, budget, today=today)


def basic_pages():
    return [
        {'url': '/activity', 'importance': 10, 'max_staleness': 2},
        {'url': '/manual', 'importance': 2, 'max_staleness': 30},
        {'url': '/broken', 'importance': 5, 'max_staleness': 3},
        {'url': '/new', 'importance': 1, 'max_staleness': 7},
    ]


class PlanningTests(unittest.TestCase):
    def test_new_pages_all_eligible_and_importance_orders_them(self):
        planner = make_planner(basic_pages())
        ranking = planner.plan(0)
        self.assertEqual([item['url'] for item in ranking],
                         ['/activity', '/broken', '/manual', '/new'])
        for item in ranking:
            self.assertTrue(item['eligible'])
            self.assertIn('urgency', item['components'])

    def test_issued_requests_are_counted_once(self):
        planner = make_planner(basic_pages(), budget=2)
        first = planner.issue('/activity', 0)
        # 同一地址在请求未返回时不能再次签发
        with self.assertRaises(PermissionError):
            planner.issue('/activity', 0)
        # 预算已扣减，重排计划不会把同一请求算两遍
        self.assertEqual(planner.remaining_budget(0), 1)
        ranking = planner.plan(0)
        self.assertNotIn('/activity', [item['url'] for item in ranking])
        planner.record(first, Observation(0, 'unchanged', digest='a1',
                                          note={'etag': 'e1'}))
        # 记账后预算不退回（请求确实发出了）；同一虚拟日内该地址也不再重选
        self.assertEqual(planner.remaining_budget(0), 1)
        self.assertNotIn('/activity',
                         [item['url'] for item in planner.plan(0)])
        # 进入下一虚拟日后可以重新参与排序
        self.assertIn('/activity',
                      [item['url'] for item in planner.plan(1)])

    def test_record_twice_is_rejected_and_release_refunds(self):
        planner = make_planner(basic_pages())
        token = planner.issue('/new', 0)
        planner.record(token, Observation(0, 'failure', status=503))
        with self.assertRaises(PermissionError):
            planner.record(token, Observation(0, 'failure', status=503))
        token2 = planner.issue('/manual', 0)
        planner.release(token2)
        self.assertEqual(planner.remaining_budget(0), 1)
        # 已释放的凭证同样不能再记账，防止幽灵请求重复计数
        with self.assertRaises(PermissionError):
            planner.record(token2, Observation(0, 'unchanged', digest='x'))

    def test_failure_does_not_refresh_content_but_causes_backoff(self):
        planner = make_planner(basic_pages())
        token = planner.issue('/broken', 0)
        planner.record(token, Observation(0, 'failure', status=503))
        self.assertIsNone(planner.pages['/broken'].last_success)
        self.assertEqual(planner.pages['/broken'].consecutive_failures, 1)
        # 首次失败退避 1 天：同一虚拟日内该地址已不可再选，理由保留退避说明
        entry = planner._score(planner.pages['/broken'], 0)
        self.assertFalse(entry['eligible'])
        self.assertTrue(any('退避' in reason for reason in entry['reasons']))
        # 退避窗口过后（第 1 天）重新可排；再次失败后退避翻倍
        self.assertTrue(next(item for item in planner.plan(1)
                             if item['url'] == '/broken')['eligible'])
        token2 = planner.issue('/broken', 1)
        planner.record(token2, Observation(1, 'failure', status=503))
        self.assertFalse(planner._score(planner.pages['/broken'], 1)
                         ['eligible'])
        # 第 2 天仍在翻倍退避窗口内（退避 2 天），第 3 天恢复
        self.assertFalse(planner._score(planner.pages['/broken'], 2)
                         ['eligible'])
        self.assertTrue(next(item for item in planner.plan(3)
                             if item['url'] == '/broken')['eligible'])

    def test_backoff_is_broken_when_deadline_overdue(self):
        planner = make_planner([
            {'url': '/broken', 'importance': 5, 'max_staleness': 2,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'd0',
                  'note': {'etag': 'e0'}}]}])
        token = planner.issue('/broken', 1)
        planner.record(token, Observation(1, 'failure', status=503))
        # 副本第 0 天取得、截止时间为第 2 天；第 1 天失败后正常应退避至第 2 天，
        # 但等到第 2 天才重试已经触及业务截止时间，允许提前重试
        entry = planner._score(planner.pages['/broken'], 1)
        self.assertTrue(entry['eligible'])
        self.assertTrue(any('提前重试' in reason for reason in entry['reasons']))
        # 若截止时间更宽松，则老老实实退避
        planner.pages['/broken'].max_staleness = 10
        entry = planner._score(planner.pages['/broken'], 1)
        self.assertFalse(entry['eligible'])

    def test_never_succeeded_page_does_not_bypass_backoff(self):
        planner = make_planner([
            {'url': '/new-broken', 'importance': 5, 'max_staleness': 2}])
        token = planner.issue('/new-broken', 0)
        planner.record(token, Observation(0, 'failure', status=503))
        # 从未取得内容的地址不享受"截止逾期打破退避"，按退避节奏探索
        entry = planner._score(planner.pages['/new-broken'], 0)
        self.assertFalse(entry['eligible'])

    def test_successful_page_is_reprioritized_lower_while_still_fresh(self):
        planner = make_planner(basic_pages(), budget=4)
        for url in ('/activity', '/manual', '/broken', '/new'):
            token = planner.issue(url, 0)
            planner.record(token, Observation(0, 'unchanged', digest='d',
                                              note={'etag': 'e'}))
        planner.today = 1
        priorities = {item['url']: item['priority']
                      for item in planner.plan(1)}
        # max_staleness=2 的活动页截止压力远大于 30 天的说明页
        self.assertGreater(priorities['/activity'], priorities['/manual'])

    def test_low_importance_page_is_not_starved_forever(self):
        pages = [
            {'url': '/hot', 'importance': 100, 'max_staleness': 2},
            {'url': '/cold', 'importance': 1, 'max_staleness': 100},
        ]
        planner = make_planner(pages, budget=1)
        # 常态排序由重要度主导：前两天预算都给热页，冷页一次都没有
        for day in range(2):
            token = planner.issue_top(day)
            self.assertEqual(token.url, '/hot')
            planner.record(token,
                           Observation(day, 'unchanged', digest='h',
                                       note={'etag': 'h'}))
        self.assertEqual(planner.pages['/cold'].observations, [])
        # 防饿死承诺是"有界等待"：冷页从未取得内容，饥饿分打满，
        # 在自身可接受陈旧时长的两倍窗口内必然被签发一次
        max_wait = 2 * planner.pages['/cold'].max_staleness
        chosen_by = None
        for day in range(2, max_wait + 1):
            if planner.plan(day)[0]['url'] == '/cold':
                # 先记录选中当日的打分（签发后该地址当日即从可排列表移除）
                cold = planner._score(planner.pages['/cold'], day)
                token = planner.issue_top(day)
                self.assertEqual(token.url, '/cold')
                planner.record(token,
                               Observation(day, 'unchanged', digest='c',
                                           note={'etag': 'c'}))
                chosen_by = day
                break
            token = planner.issue_top(day)
            planner.record(token,
                           Observation(day, 'unchanged', digest='h',
                                       note={'etag': 'h'}))
        self.assertIsNotNone(
            chosen_by, f'冷页在 {max_wait} 天窗口内没有获得任何预算')
        self.assertLessEqual(chosen_by, max_wait)
        # 被选中当日冷页饥饿打满、强保底生效，排序理由可解释
        self.assertEqual(cold['components']['starvation'], 1.0)
        self.assertEqual(cold['components']['starvation_guarantee'],
                         STARVATION_GUARANTEE)
        self.assertTrue(any('饥饿' in reason for reason in cold['reasons']))

    def test_exploration_weight_applies_only_without_content_evidence(self):
        planner = make_planner(basic_pages())
        token = planner.issue('/new', 0)
        planner.record(token, Observation(0, 'unchanged', digest='n',
                                          note={'etag': 'e'}))
        entry = next(item for item in planner.plan(1)
                     if item['url'] == '/new')
        self.assertEqual(entry['components']['explore'], 0.0)
        fresh_entry = next(item for item in planner.plan(1)
                           if item['url'] == '/activity')
        self.assertEqual(fresh_entry['components']['explore'], 1.0)


class IdentityTests(unittest.TestCase):
    def _planner(self):
        return make_planner([
            {'url': '/old', 'importance': 4, 'max_staleness': 3,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'd1',
                  'note': {'etag': 'e1'}},
                 {'time': 1, 'kind': 'changed', 'digest': 'd2',
                  'note': {'etag': 'e2'}},
             ]},
        ])

    def test_permanent_redirect_inherits_history_and_rate_estimate(self):
        planner = self._planner()
        target = planner.migrate_identity('/old', '/new-loc', 2)
        self.assertEqual(target.url, '/new-loc')
        self.assertEqual(
            [item.digest for item in target.observations if item.digest],
            ['d1', 'd2'])
        self.assertTrue(all(item.origin == '/old'
                            for item in target.observations))
        self.assertEqual(target.rate_info['changed_intervals'], 1)
        self.assertTrue(planner.pages['/old'].retired)
        self.assertEqual(planner.pages['/old'].alias_of, '/new-loc')
        # 退休地址不再参与计划
        self.assertNotIn('/old',
                         [item['url'] for item in planner.plan(2)])

    def test_temporary_redirect_keeps_identity_separate(self):
        planner = self._planner()
        result = planner.migrate_identity('/old', '/tmp', 2,
                                          permanent=False)
        self.assertIsNone(result)
        self.assertFalse(planner.pages['/old'].retired)
        self.assertNotIn('/tmp', planner.pages)

    def test_equal_digest_never_merges_distinct_histories(self):
        # /a 与 /b 正文摘要完全相同，但没有任何重定向证据：
        # 计划器没有提供、也不允许通过摘要合并身份的入口
        planner = make_planner([
            {'url': '/a', 'importance': 3, 'max_staleness': 5,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'same',
                  'note': {'etag': 'ea'}}]},
            {'url': '/b', 'importance': 7, 'max_staleness': 5,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'same',
                  'note': {'etag': 'eb'}}]},
        ])
        self.assertEqual(set(planner.pages), {'/a', '/b'})
        self.assertEqual(planner.pages['/a'].rate_info['exposure_days'],
                         planner.pages['/b'].rate_info['exposure_days'])

    def test_migration_into_existing_page_pools_evidence_chronologically(self):
        planner = make_planner([
            {'url': '/old', 'importance': 4, 'max_staleness': 3,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'd1',
                  'note': {'etag': 'e1'}}]},
            {'url': '/existing', 'importance': 4, 'max_staleness': 3,
             'observations': [
                 {'time': 2, 'kind': 'unchanged', 'digest': 'x',
                  'note': {'etag': 'ex'}}]},
        ])
        target = planner.migrate_identity('/old', '/existing', 3)
        self.assertEqual([item.time for item in target.observations
                          if item.kind != 'redirect'], [0, 2])


class AuditTests(unittest.TestCase):
    def test_unmet_targets_report_overdue_and_never_fetched(self):
        planner = make_planner([
            {'url': '/ok', 'importance': 9, 'max_staleness': 10,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'd',
                  'note': {'etag': 'e'}}]},
            {'url': '/stale', 'importance': 8, 'max_staleness': 2,
             'observations': [
                 {'time': 0, 'kind': 'unchanged', 'digest': 'd',
                  'note': {'etag': 'e'}}]},
            {'url': '/unknown', 'importance': 1, 'max_staleness': 5},
        ])
        report = planner.unmet_targets(5)
        urls = {item['url']: item for item in report}
        self.assertNotIn('/ok', urls)
        self.assertEqual(urls['/stale']['overdue_days'], 3)
        self.assertIsNone(urls['/unknown']['overdue_days'])

    def test_budget_exhaustion_blocks_issue_but_plan_still_explains(self):
        planner = make_planner(basic_pages(), budget=1)
        self.assertIsNotNone(planner.issue_top(0))
        self.assertIsNone(planner.issue_top(0))
        # 计划视图仍完整展示未满足的排序，便于解释资源缺口
        self.assertEqual(len(planner.plan(0)), 3)

    def test_retirement_can_leave_budget_unused_without_recycling(self):
        planner = make_planner([
            {'url': '/only', 'importance': 5, 'max_staleness': 3}], budget=2)
        token = planner.issue_top(0)
        planner.record(token, Observation(0, 'unchanged', digest='d',
                                          note={'etag': 'e'}))
        planner.migrate_identity('/only', '/moved', 0)
        # 旧地址退休退出候选；新身份继承"当日已访问"状态，当日不再签发，
        # 剩余预算明确闲置，不会被伪造请求花掉
        self.assertEqual(planner.plan(0), [])
        self.assertIsNone(planner.issue_top(0))
        self.assertEqual(planner.remaining_budget(0), 1)
        # 进入下一虚拟日，继承了历史的新地址正常参与排序
        self.assertEqual(
            [item['url'] for item in planner.plan(1)], ['/moved'])

    def test_negative_clock_rejected(self):
        planner = make_planner(basic_pages())
        with self.assertRaises(ValueError):
            planner.plan(-1)
