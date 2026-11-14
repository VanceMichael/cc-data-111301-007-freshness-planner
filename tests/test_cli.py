import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CommandTests(unittest.TestCase):
    def test_preview_and_raw_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable,'-m','freshness_planner','demo'],capture_output=True,text=True,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            result = json.loads(process.stdout)
            self.assertIn('资料预览', result)
            self.assertGreater(result['原始资料']['bytes'], 0)
            self.assertEqual(len(result['原始资料']['sha256']), 64)

    def test_plan_command(self):
        process = subprocess.run(
            [sys.executable, '-m', 'freshness_planner', 'plan',
             'fixtures/pages.json'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['daily_budget'], 2)
        self.assertTrue(result['ranking'])
        self.assertIn('components', result['ranking'][0])

    def test_rehearse_command(self):
        process = subprocess.run(
            [sys.executable, '-m', 'freshness_planner', 'rehearse'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(len(result['days']), 7)
        for day in result['days']:
            self.assertLessEqual(len(day['visits']), result['daily_budget'])
        self.assertIn('evaluation', result)
