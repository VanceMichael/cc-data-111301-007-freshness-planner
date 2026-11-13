from pathlib import Path
import json
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from freshness_planner.http_source import source_server


class HttpSourceTests(unittest.TestCase):
    def test_routes_and_control(self):
        routes = json.loads(Path('fixtures/routes.json').read_text(encoding='utf-8'))
        route = next(iter(routes))
        first = routes[route][0] if isinstance(routes[route],list) else routes[route]
        with source_server('fixtures') as (address, state):
            with urlopen(address+route,timeout=3) as response:
                self.assertEqual(response.read(), (Path('fixtures')/first['file']).read_bytes())
            with urlopen(Request(address+'/control/advance',b''),timeout=3) as response:
                self.assertEqual(json.load(response)['revision'], 1)
            self.assertEqual(state['revision'], 1)
            with self.assertRaises(HTTPError) as failure:
                urlopen(address+'/missing',timeout=3)
            self.assertEqual(failure.exception.code,404)
