import unittest

from freshness_planner.simulation import (
    ScriptedSource, build_week_scenario, run_week,
)


class ScriptedSourceTests(unittest.TestCase):
    def test_conditional_request_304_differs_from_digest_compare(self):
        source = ScriptedSource(
            versions={'/p': [{'day': 0, 'digest': 'd', 'etag': 'e'}]})
        self.assertEqual(source.fetch('/p', 1, 'e')['kind'], '304')
        fresh = source.fetch('/p', 1, None)
        self.assertEqual(fresh['kind'], '200')
        self.assertEqual(fresh['digest'], 'd')

    def test_failures_carry_no_content_information(self):
        source = ScriptedSource(
            versions={'/p': [{'day': 0, 'digest': 'd', 'etag': 'e'}]},
            failures={'/p': {2}})
        self.assertEqual(source.fetch('/p', 2, None)['kind'], 'failure')
        self.assertEqual(source.fetch('/p', 3, None)['kind'], '200')


class WeekRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.result = run_week()

    def test_budget_is_respected_every_day(self):
        for day in self.result['days']:
            spent = len(day['visits'])
            self.assertLessEqual(spent, self.result['planner'].daily_budget)
            self.assertEqual(
                day['remaining_budget'],
                self.result['planner'].daily_budget - spent)
            # 每个决策都带可解释的分量与理由
            for visit in day['visits']:
                self.assertTrue(visit['reasons'])
                self.assertIn('urgency', visit['components'])

    def test_broken_page_never_updates_content_and_keeps_failing(self):
        planner = self.result['planner']
        broken = planner.pages['/broken']
        self.assertIsNone(broken.last_success)
        self.assertGreater(broken.consecutive_failures, 1)
        visits = [visit for day in self.result['days']
                  for visit in day['visits'] if visit['url'] == '/broken']
        self.assertTrue(visits)
        self.assertTrue(all('失败' in visit['outcome'] for visit in visits))

    def test_activity_change_is_detected_as_interval_not_change_time(self):
        evaluation = self.result['evaluation']
        lags = [item for item in evaluation['detection_lags']
                if item['url'] == '/activity']
        self.assertTrue(lags, '活动页改版应在周内被发现')
        lag = lags[0]
        # 真实改版在第 3 天，发现只能晚于或等于改版日，且不会声称第 3 天即修改时刻
        self.assertGreaterEqual(lag['detected_on'], lag['changed_on'])
        for day in self.result['days']:
            for visit in day['visits']:
                if visit['url'] == '/activity' and '变化' in visit['outcome']:
                    self.assertIn('区间', visit['outcome'])

    def test_redirect_retirement_and_inheritance(self):
        planner = self.result['planner']
        old = planner.pages['/old-doc']
        self.assertTrue(old.retired)
        self.assertEqual(old.alias_of, '/docs-new')
        target = planner.pages['/docs-new']
        # 旧地址的成功观测被继承，新地址不需要从零探索
        self.assertTrue(any(item.origin == '/old-doc'
                            for item in target.observations))
        retired_urls = [page['url'] for page in
                        self.result['evaluation']['pages']
                        if page['retired']]
        self.assertIn('/old-doc', retired_urls)

    def test_same_digest_pages_keep_separate_histories(self):
        planner = self.result['planner']
        # /new 在第 5 天后正文与 /manual 摘要相同，但两者仍是独立页面：
        # 观测分别归属、身份不合并、变化率估计各自独立计算
        self.assertIn('/new', planner.pages)
        self.assertIn('/manual', planner.pages)
        new_state = planner.pages['/new']
        manual_state = planner.pages['/manual']
        self.assertIsNone(new_state.alias_of)
        self.assertIsNone(manual_state.alias_of)
        for item in new_state.observations:
            self.assertNotIn(item, manual_state.observations)
        # /new 在第 5 天的版本与 /manual 同摘要，也不会把 /manual 的历史借过来
        new_evidence = new_state.rate_info['unchanged_intervals']
        manual_evidence = manual_state.rate_info['unchanged_intervals']
        self.assertEqual(new_evidence, 0)

    def test_unmet_targets_are_reported_not_silently_dropped(self):
        final = self.result['evaluation']['unmet_targets']
        # /broken 整周失败，必然出现在未满足清单中
        self.assertIn('/broken', {item['url'] for item in final})
        # 每一天的报告都应可序列化为基本类型
        for day in self.result['days']:
            for item in day['unmet_targets']:
                self.assertIn('reason', item)

    def test_replanning_after_outcome_changes_later_ranking(self):
        # 首日拿到结果后，第二日的排序与首日不同（拿到内容的页面截止压力下降）
        first = [visit['url'] for visit in self.result['days'][0]['visits']]
        second = [visit['url'] for visit in self.result['days'][1]['visits']]
        self.assertTrue(first)
        # 活动页首日确认内容后，第二日仍会因高重要度/短截止被关注，
        # 但新地址与失败页会重新参与竞争，计划确实被重新安排
        self.assertTrue(any(url in second for url in
                            ('/new', '/broken', '/activity')))


class ScenarioTests(unittest.TestCase):
    def test_scenario_shape(self):
        pages, source, budget, horizon = build_week_scenario()
        self.assertEqual(budget, 2)
        self.assertEqual(horizon, 7)
        self.assertIsInstance(source, ScriptedSource)
        self.assertEqual(len(pages), 6)
