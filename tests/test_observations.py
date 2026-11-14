import math
import unittest

from freshness_planner.observations import (
    Observation, build_intervals, estimate_rate, probability_changed,
)


def changed(at):
    return Observation(at, 'changed', digest=f'd{at}')


def same(at, digest='d0'):
    return Observation(at, 'unchanged', digest=digest)


class IntervalTests(unittest.TestCase):
    def test_change_time_is_censored_not_assumed(self):
        # 第 0 天抓取后，第 5 天才抓到变化：只能得到一个"区间内变化"的证据，
        # 观测里不产生任何"第 5 天是修改时刻"的字段。
        intervals = build_intervals([same(0), changed(5)])
        self.assertEqual(intervals, [(5, True)])

    def test_304_and_same_digest_are_separate_evidence_but_both_zero_change(self):
        intervals = build_intervals([
            Observation(0, 'unchanged', digest='x'),
            Observation(2, '304', digest='x'),
            Observation(4, 'unchanged', digest='x'),
        ])
        self.assertEqual(intervals, [(2, False), (2, False)])

    def test_failures_and_legacy_do_not_create_content_intervals(self):
        intervals = build_intervals([
            same(0),
            Observation(1, 'failure', status=503),
            Observation(2, 'legacy'),
            Observation(4, 'unchanged', digest='d0'),
        ])
        # 区间内部夹着失败/legacy 时内容状态无法闭合，不产生任何证据
        self.assertEqual(intervals, [])
        # 失败之后重新建立基线，之后的区间才可以正常闭合
        intervals = build_intervals([
            same(0),
            Observation(1, 'failure', status=503),
            same(3), changed(4),
        ])
        self.assertEqual(intervals, [(1, True)])

    def test_invalid_observations_are_rejected(self):
        with self.assertRaises(ValueError):
            Observation(1, 'redirect')
        with self.assertRaises(ValueError):
            Observation(-1, 'unchanged', digest='x')


class RateEstimateTests(unittest.TestCase):
    def test_prior_only_for_new_page(self):
        info = estimate_rate([])
        self.assertEqual(info['basis'], 'prior_only')
        self.assertGreater(info['rate'], 0)

    def test_long_quiet_history_pushes_rate_to_floor(self):
        info = estimate_rate([same(0), same(100), same(200)])
        self.assertEqual(info['basis'], 'no_change_observed')
        self.assertLess(info['rate'], 0.001)

    def test_more_changes_in_same_exposure_means_higher_rate(self):
        slow = estimate_rate([same(0), changed(10), changed(20)])
        fast = estimate_rate([same(0), changed(2), changed(4), changed(6)])
        self.assertLess(slow['rate'], fast['rate'])

    def test_estimate_ignores_calendar_gaps_without_endpoints(self):
        # 中间失败 30 天使 0..12 区间无法闭合；失败之后的干净区间仍可估计，
        # 失败本身既不会被当成"无变化"，也不会产生虚假的暴露时间
        with_failures = estimate_rate([
            same(0), Observation(5, 'failure', status=503),
            Observation(10, 'failure', status=503), same(12), changed(15)])
        clean = estimate_rate([same(12), changed(15)])
        self.assertEqual(with_failures['exposure_days'],
                         clean['exposure_days'])
        self.assertAlmostEqual(with_failures['rate'], clean['rate'])

    def test_probability_is_monotone(self):
        rate = 0.1
        self.assertEqual(probability_changed(rate, 0), 0.0)
        self.assertLess(probability_changed(rate, 1),
                        probability_changed(rate, 10))
        self.assertLessEqual(probability_changed(rate, 1000), 1.0)
        self.assertAlmostEqual(probability_changed(rate, 10),
                               1 - math.exp(-1))
