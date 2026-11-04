import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import dashboard as d
import server

NOW = dt.datetime(2026, 11, 4, tzinfo=d.UTC)


def config(**overrides):
    result = dict(mode='local', grant_total=68, grant_start='2026-10-10',
                  expiry='2026-11-09', sessions_dir='/nonexistent-test-sessions',
                  log_file=None, pricing_file=str(d.ROOT / 'pricing.json'), port=8765)
    result.update(overrides)
    return result


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.rates = d.load_pricing(d.ROOT / 'pricing.json')

    def test_cached_tokens_and_reasoning_not_double_billed(self):
        usage = dict(input_tokens=1_000_000, cached_input_tokens=200_000,
                     output_tokens=100_000, reasoning_output_tokens=50_000)
        self.assertAlmostEqual(d.estimate_cost('gpt-4.1', usage, self.rates), 2.5)

    def test_fallback_and_dated_snapshot(self):
        self.assertEqual(d.pricing_model('gpt-4.1-2025-04-14', self.rates), 'gpt-4.1')
        self.assertEqual(d.pricing_model('gpt-4.1-unknown', self.rates), 'default')
        self.assertAlmostEqual(d.estimate_cost('unknown', dict(input_tokens=1_000_000), self.rates), 1.25)

    def test_cached_tokens_clamped(self):
        self.assertAlmostEqual(d.estimate_cost('gpt-4.1', dict(input_tokens=100,
                               cached_input_tokens=200), self.rates), .00005)


class LocalTests(unittest.TestCase):
    def test_fixture_deltas_repeated_totals_model_and_reset(self):
        warnings = set()
        rows = list(d.parse_session(d.ROOT / 'tests/fixtures/session.jsonl', warnings))
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r['input_tokens'] for r in rows), 1900)
        self.assertEqual(rows[0]['model'], 'gpt-4.1-mini')
        self.assertEqual(rows[0]['project'], 'sample-project')
        self.assertEqual(rows[1]['project'], 'second-project')
        self.assertEqual(rows[1]['input_tokens'], 600)
        self.assertEqual(rows[-1]['day'], '2026-11-02')  # UTC
        self.assertTrue(warnings)

    def test_fork_first_event_uses_last_not_inherited_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fork.jsonl'
            p.write_text(json.dumps(dict(timestamp='2026-11-01', type='event_msg', payload=dict(
                type='token_count', info=dict(total_token_usage=dict(input_tokens=10000),
                                             last_token_usage=dict(input_tokens=50)))))+'\n')
            self.assertEqual(list(d.parse_session(p))[0]['input_tokens'], 50)

    def test_optional_log_filtering_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'extra.jsonl'
            rows = [dict(ts='2026-11-01', model='gpt-4.1', input_tokens=1_000_000,
                         cached_input_tokens=200_000, output_tokens=100_000, project='demo'),
                    dict(ts='2026-09-01', model='unknown', output_tokens=999),
                    dict(ts='2026-11-20', model='unknown', output_tokens=999)]
            p.write_text('\n'.join(map(json.dumps, rows)))
            s = d.build_summary(config(sessions_dir=tmp, log_file=str(p)), NOW)
            self.assertEqual(s['spent'], 2.5)
            self.assertEqual(s['remaining'], 65.5)
            self.assertEqual(s['by_project'][0]['name'], 'demo')
            self.assertEqual(s['tokens']['input_tokens'], 1_000_000)
            self.assertTrue(s['estimated'])
            self.assertEqual(len(s['daily']), 26)
            self.assertEqual(s['fallback_models'], [])


class ProjectionTests(unittest.TestCase):
    def test_zero_days_count_and_today_excluded_from_burn(self):
        daily = {'2026-11-01': 14, '2026-11-04': 6}
        p = d.projection(daily, 68, '2026-10-10', '2026-11-09', NOW)
        self.assertEqual(p['spent'], 20)
        self.assertEqual(p['avg_daily_burn'], 2)
        self.assertEqual(p['remaining'], 48)
        self.assertEqual(p['projected_spend'], 30)
        self.assertEqual(p['unused_at_expiry'], 38)
        self.assertEqual(p['days_left'], 5)
        self.assertEqual(p['message'], '~$38.00 will expire unused')

    def test_depletion_expired_exhausted_and_future_grant(self):
        p = d.projection({'2026-11-01': 35}, 50, '2026-10-10', '2026-11-09', NOW)
        self.assertEqual(p['depletion_date'], '2026-11-07')
        self.assertIn('will use it up by', p['message'])
        p = d.projection({'2026-11-01': 75}, 50, '2026-10-10', '2026-11-09', NOW)
        self.assertEqual(p['remaining'], 0)
        self.assertEqual(p['percent_used'], 150)
        self.assertEqual(p['status'], 'exhausted')
        p = d.projection({}, 50, '2026-10-01', '2026-11-01', NOW)
        self.assertEqual(p['status'], 'expired')
        self.assertEqual(p['days_left'], 0)
        p = d.projection({}, 50, '2026-11-05', '2026-11-09', NOW)
        self.assertEqual(p['spent'], 0)
        self.assertEqual(p['avg_daily_burn'], 0)

    def test_new_grant_and_exact_expiry_boundary(self):
        p = d.projection({'2026-11-03': 4, '2026-11-04': 99}, 200,
                         '2026-11-03', '2026-11-04', NOW)
        self.assertEqual(p['spent'], 4)
        self.assertEqual(p['avg_daily_burn'], 4)
        self.assertEqual(p['burn_sample_days'], 1)


class OfficialTests(unittest.TestCase):
    @staticmethod
    def page(results, more=False, next_page=None):
        return dict(data=[dict(start_time=int(d.timestamp('2026-11-01').timestamp()),
                               results=results)], has_more=more, next_page=next_page)

    def test_pagination_both_endpoints_and_authoritative_costs(self):
        calls = []
        costs = [self.page([dict(amount=dict(value=12, currency='usd'), project_id='p1',
                                line_item='gpt-4.1 input')], True, 'cost-page-2'),
                 self.page([dict(amount=dict(value=3, currency='usd'), project_id='p2')])]
        usage = [self.page([dict(model='gpt-4.1', input_tokens=1_000_000,
                                 input_cached_tokens=200_000, output_tokens=100_000)], True, 'usage-page-2'),
                 self.page([dict(model='gpt-4.1-mini', input_tokens=100, output_tokens=50)])]

        def open_request(request, timeout):
            calls.append(request)
            page = costs.pop(0) if '/costs?' in request.full_url else usage.pop(0)
            return io.BytesIO(json.dumps(page).encode())

        with patch.dict(os.environ, {'OPENAI_ADMIN_KEY': 'test-secret'}), patch(
                'dashboard.urllib.request.OpenerDirector.open', side_effect=open_request):
            s = d.build_summary(config(mode='official'), NOW)
        self.assertEqual(len(calls), 4)
        self.assertEqual(s['spent'], 15)
        self.assertEqual(s['remaining'], 53)
        self.assertFalse(s['estimated'])
        self.assertTrue(s['by_model'][0]['estimated'])
        self.assertEqual(s['by_project'][0]['cost'], 12)
        self.assertNotIn('test-secret', json.dumps(s))
        for i, request in enumerate(calls):
            url = urllib.parse.urlsplit(request.full_url)
            q = urllib.parse.parse_qs(url.query)
            self.assertEqual(url.netloc, 'api.openai.com')
            self.assertEqual(q['bucket_width'], ['1d'])
            self.assertEqual(q['limit'], ['31'])
            self.assertEqual(q['group_by'], ['line_item', 'project_id'] if i < 2 else ['model', 'project_id'])
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')
            if i % 2:
                self.assertIn('page', q)

    def test_401_403_safe_actionable_error(self):
        for code in (401, 403):
            with self.subTest(code=code), patch.dict(os.environ, {'OPENAI_ADMIN_KEY': 'test-secret'}), patch(
                    'dashboard.urllib.request.OpenerDirector.open', side_effect=urllib.error.HTTPError(
                        'https://api.openai.com', code, 'test-secret', {}, io.BytesIO(b'test-secret'))):
                with self.assertRaises(d.DashboardError) as caught:
                    d.build_summary(config(mode='official'), NOW)
                self.assertEqual(str(caught.exception), d.ADMIN_ERROR)
                self.assertNotIn('test-secret', str(caught.exception))

    def test_missing_key_auto_and_redirect(self):
        with patch.dict(os.environ, {'OPENAI_ADMIN_KEY': ''}):
            self.assertEqual(d.build_summary(config(mode='auto'), NOW)['mode'], 'local')
            with self.assertRaises(d.DashboardError):
                d.build_summary(config(mode='official'), NOW)
        self.assertIsNone(d.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))

    def test_repeated_pagination_is_rejected(self):
        page = self.page([], True, 'repeated')
        with patch('dashboard.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k:
                   io.BytesIO(json.dumps(page).encode())):
            with self.assertRaisesRegex(d.DashboardError, 'pagination'):
                list(d.official_buckets('costs', 'secret', 1, 2, ['line_item']))


class ConfigAndHTTPTests(unittest.TestCase):
    def test_env_is_literal_and_does_not_override_existing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EXISTING': 'keep'}):
            p = Path(tmp) / '.env'
            p.write_text('# comment\nEXISTING=replace\nNEW_TEST_VALUE="$(never-execute)"\n')
            d.load_env(p)
            self.assertEqual(os.environ['EXISTING'], 'keep')
            self.assertEqual(os.environ['NEW_TEST_VALUE'], '$(never-execute)')

    def test_config_precedence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CREDIT_GRANT_TOTAL': '100'}):
            p = Path(tmp) / 'config.json'
            p.write_text('{"grant_total": 50}')
            args = argparse.Namespace(config=str(p), grant_total=68)
            self.assertEqual(d.configuration(args)['grant_total'], 68)
            p.write_text('{"OPENAI_ADMIN_KEY": "never-configure-here"}')
            with self.assertRaises(d.DashboardError):
                d.configuration(args)

    def test_http_allowlist_json_and_host_protection(self):
        cfg = config()
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.handler_for(cfg))
        cfg['port'] = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % httpd.server_port
        try:
            with patch('server.build_summary', return_value={'app':'openai-credit-dashboard'}):
                with urllib.request.urlopen(base + '/api/summary') as r:
                    self.assertEqual(json.load(r)['app'], 'openai-credit-dashboard')
                    self.assertEqual(r.headers['Cache-Control'], 'no-store')
            for path in ('/.env', '/config.json', '/dashboard.py', '/../.env'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + path)
                self.assertEqual(caught.exception.code, 404)
            request = urllib.request.Request(base, headers={'Host':'attacker.test'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
