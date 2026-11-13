import unittest
from freshness_planner.core import fixed_cycle


class CycleTests(unittest.TestCase):
    def test_due_boundary(self):
        page={'url':'/a','last_visit':1,'interval':7}
        self.assertEqual(fixed_cycle([page],7),[])
        self.assertEqual(fixed_cycle([page],8),['/a'])

    def test_new_page_and_invalid_clock(self):
        self.assertEqual(fixed_cycle([{'url':'/new','last_visit':None,'interval':7}],0),['/new'])
        with self.assertRaises(ValueError):
            fixed_cycle([],-1)
