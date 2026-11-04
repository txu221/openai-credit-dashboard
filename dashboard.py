"""Data collection and grant accounting. Python standard library only."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent
ADMIN_ERROR = ('key lacks admin permission; create an Admin key at '
               'platform.openai.com/settings/organization/admin-keys')
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')


class DashboardError(Exception):
    """A safe, user-facing error; never include request headers or response bodies."""


def load_env(path):
    """Small .env reader: literal values, no shell evaluation or interpolation."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, sep, value = line.partition('=')
        if sep and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise DashboardError('Cannot read JSON file; check its path and JSON syntax.') from None


def configuration(args):
    load_env(ROOT / '.env')
    now = dt.datetime.now(UTC).date()
    config = dict(mode='auto', grant_total=200, grant_start=now.replace(day=1).isoformat(),
                  expiry=(now + dt.timedelta(days=30)).isoformat(),
                  sessions_dir=str(Path.home() / '.codex/sessions'), log_file=None,
                  pricing_file=str(ROOT / 'pricing.json'), port=8765)
    path = Path(args.config).expanduser()
    if path.exists():
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise DashboardError('Config must be a JSON object.')
        if any('key' in k.lower() or 'secret' in k.lower() for k in raw):
            raise DashboardError('Store keys only in OPENAI_ADMIN_KEY in the environment or .env.')
        unknown = set(raw) - set(config)
        if unknown:
            raise DashboardError('Config contains unsupported fields; see config.example.json.')
        config.update(raw)
    elif args.config != str(ROOT / 'config.json'):
        raise DashboardError('Config file does not exist.')
    for key in config:
        env = os.environ.get('CREDIT_' + key.upper())
        if env is not None:
            config[key] = env
        cli = getattr(args, key, None)
        if cli is not None:
            config[key] = cli
    try:
        config['grant_total'] = float(config['grant_total'])
        config['port'] = int(config['port'])
        start = dt.date.fromisoformat(config['grant_start'])
        end = dt.date.fromisoformat(config['expiry'])
        if (not math.isfinite(config['grant_total']) or config['grant_total'] <= 0
                or end <= start or not 1 <= config['port'] <= 65535
                or config['mode'] not in ('auto', 'local', 'official')):
            raise ValueError
    except (ValueError, TypeError):
        raise DashboardError('Invalid configuration: use a positive grant, valid port/mode, '
                             'and YYYY-MM-DD dates with expiry after grant_start.') from None
    for key in ('sessions_dir', 'log_file', 'pricing_file'):
        if config[key]:
            config[key] = str(Path(config[key]).expanduser())
    return config


def load_pricing(path):
    data = read_json(path)
    try:
        rates = data['models']
        assert 'default' in rates
        for rate in rates.values():
            for key in ('input', 'cached_input', 'output'):
                assert math.isfinite(float(rate[key])) and float(rate[key]) >= 0
    except (KeyError, TypeError, ValueError, AssertionError, AttributeError):
        raise DashboardError('Invalid pricing file: models needs default and nonnegative '
                             'input, cached_input, output rates per million tokens.') from None
    return rates


def pricing_model(model, rates):
    if model in rates:
        return model
    # Only strip dated snapshots; unknown model variants must use the visible fallback.
    base = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', model)
    return base if base in rates else 'default'


def tokens(raw):
    return {key: max(0, int(raw.get(key) or 0)) for key in TOKEN_FIELDS}


def estimate_cost(model, usage, rates):
    rate = rates[pricing_model(model, rates)]
    t = tokens(usage)
    cached = min(t['cached_input_tokens'], t['input_tokens'])
    return ((t['input_tokens'] - cached) * float(rate['input'])
            + cached * float(rate['cached_input'])
            + t['output_tokens'] * float(rate['output'])) / 1_000_000


def timestamp(value):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, UTC)
    result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def json_lines(path, warnings):
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError
                    yield value
                except ValueError:
                    warnings.add('Some malformed JSONL records were skipped.')
    except OSError:
        warnings.add('Some local log files could not be read.')


def parse_session(path, warnings=None):
    """Yield usage deltas, never conversation content. Ignore repeated totals."""
    if warnings is None:
        warnings = set()
    model, project, previous = 'unknown', 'unknown', None
    for event in json_lines(path, warnings):
        try:
            payload = event.get('payload') or {}
            kind = event.get('type')
            if kind in ('session_meta', 'turn_context'):
                model = payload.get('model') or model
                cwd = payload.get('cwd')
                if cwd:
                    project = str(cwd).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or 'root'
                continue
            if kind != 'event_msg' or payload.get('type') != 'token_count':
                continue
            info = payload.get('info') or {}
            total, last = info.get('total_token_usage'), info.get('last_token_usage')
            if total is not None:
                current = tokens(total)
                if previous is None:
                    # A fork may start with inherited totals; use its own last response.
                    usage = tokens(last) if last is not None else current
                elif any(current[k] < previous[k] for k in ('input_tokens', 'output_tokens')):
                    # Counter resets begin a new segment.
                    usage = tokens(last) if last is not None else current
                else:
                    usage = {k: max(0, current[k] - previous[k]) for k in TOKEN_FIELDS}
                previous = current
            elif last is not None:
                usage = tokens(last)
            else:
                continue
            if not any(usage.values()):
                continue
            yield dict(day=timestamp(event['timestamp']).date().isoformat(),
                       model=model, project=project, **usage)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            warnings.add('Some invalid session records were skipped.')


def local_records(config, warnings):
    directory = Path(config['sessions_dir'])
    if not directory.exists():
        warnings.add('Codex sessions directory was not found; add a local JSONL log or configure sessions_dir.')
    # File copies of the same session name are common after backups.
    seen = set()
    for path in sorted(directory.rglob('*.jsonl')):
        if path.name in seen:
            continue
        seen.add(path.name)
        yield from parse_session(path, warnings)
    if config.get('log_file'):
        for row in json_lines(config['log_file'], warnings):
            try:
                yield dict(day=timestamp(row['ts']).date().isoformat(),
                           model=str(row.get('model') or 'unknown'),
                           project=str(row.get('project') or 'unknown'), **tokens(row))
            except (KeyError, TypeError, ValueError, OverflowError):
                warnings.add('Some invalid local usage records were skipped.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never be forwarded to a redirect destination.
        return None


def official_buckets(resource, key, start, end, groups):
    if resource not in ('costs', 'usage/completions'):
        raise DashboardError('Unsupported OpenAI endpoint.')
    opener = urllib.request.build_opener(NoRedirect())
    page, seen = None, set()
    while True:
        params = [('start_time', start), ('end_time', end), ('bucket_width', '1d'),
                  ('limit', 31)] + [('group_by', group) for group in groups]
        if page:
            params.append(('page', page))
        url = 'https://api.openai.com/v1/organization/' + resource + '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key,
                                                      'User-Agent': 'openai-credit-dashboard/1.0'})
        try:
            with opener.open(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DashboardError(ADMIN_ERROR) from None
            raise DashboardError('OpenAI API returned HTTP %d; retry later or check your organization access.'
                                 % exc.code) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise DashboardError('Cannot reach OpenAI API or received invalid JSON; check your connection and retry.') from None
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise DashboardError('OpenAI API returned an unexpected response.')
        yield from data['data']
        page = data.get('next_page')
        if not data.get('has_more'):
            break
        if not isinstance(page, str) or not page or page in seen:
            raise DashboardError('OpenAI API returned invalid pagination.')
        seen.add(page)


def projection(daily, grant_total, start, expiry, now=None):
    now = now or dt.datetime.now(UTC)
    today = now.date()
    start, expiry = dt.date.fromisoformat(start), dt.date.fromisoformat(expiry)
    spent = sum(value for day, value in daily.items() if start <= dt.date.fromisoformat(day) < expiry
                and dt.date.fromisoformat(day) <= today)
    # Seven completed UTC days, including zero-spend days; shorten for a new grant.
    window_end = min(today, expiry)
    window_start = max(start, window_end - dt.timedelta(days=7))
    sample_days = max(0, (window_end - window_start).days)
    burn = sum(value for day, value in daily.items()
               if window_start <= dt.date.fromisoformat(day) < window_end) / max(1, sample_days)
    seconds_left = max(0, (dt.datetime.combine(expiry, dt.time(), UTC) - now).total_seconds())
    future_days = max(0, (expiry - max(today, start)).days)
    # Today's recorded spend is already included; prorate only the rest of today.
    if start <= today < expiry:
        future_days = seconds_left / 86400
    remaining = max(0, grant_total - spent)
    projected = spent + burn * future_days
    depletion = None
    if remaining == 0:
        depletion = today.isoformat()
    elif burn > 0 and projected >= grant_total:
        depletion = (max(now, dt.datetime.combine(start, dt.time(), UTC))
                     + dt.timedelta(days=remaining / burn)).date().isoformat()
    unused = max(0, grant_total - projected)
    if today >= expiry:
        status, message = 'expired', '~$%.2f expired unused' % remaining
    elif remaining == 0:
        status, message = 'exhausted', 'Credit used up'
    elif depletion:
        status, message = 'depletes', 'will use it up by ' + depletion
    else:
        status, message = 'unused', '~$%.2f will expire unused' % unused
    return dict(spent=round(spent, 6), remaining=round(remaining, 6),
                percent_used=round(spent / grant_total * 100, 2),
                avg_daily_burn=round(burn, 6), burn_sample_days=sample_days,
                projected_spend=round(projected, 6), unused_at_expiry=round(unused, 6),
                depletion_date=depletion, days_left=math.ceil(seconds_left / 86400),
                seconds_left=int(seconds_left), status=status, message=message)


def build_summary(config, now=None):
    now = now or dt.datetime.now(UTC)
    rates = load_pricing(config['pricing_file'])
    mode = config['mode']
    key = os.environ.get('OPENAI_ADMIN_KEY', '').strip()
    if mode == 'auto':
        mode = 'official' if key else 'local'
    warnings, fallback = set(), set()
    daily, models, projects, line_items = {}, {}, {}, {}
    token_total = {k: 0 for k in TOKEN_FIELDS}
    start = config['grant_start']
    end_day = min(config['expiry'], (now.date() + dt.timedelta(days=1)).isoformat())

    def add(target, name, cost):
        target[name] = target.get(name, 0) + cost

    def usage_record(row):
        model = row['model']
        if pricing_model(model, rates) == 'default':
            fallback.add(model)
        cost = estimate_cost(model, row, rates)
        add(models, model, cost)
        for field in TOKEN_FIELDS:
            token_total[field] += row[field]
        return cost

    if mode == 'official':
        if not key:
            raise DashboardError('Official mode requires OPENAI_ADMIN_KEY. ' + ADMIN_ERROR)
        if start < end_day:
            start_ts = int(timestamp(start).timestamp())
            end_ts = min(int(timestamp(config['expiry']).timestamp()), int(now.timestamp()))
            try:
                for bucket in official_buckets('costs', key, start_ts, end_ts, ['line_item', 'project_id']):
                    day = timestamp(bucket['start_time']).date().isoformat()
                    if not start <= day < end_day:
                        continue
                    for row in bucket['results']:
                        amount = row.get('amount') or {}
                        if amount.get('currency', 'usd').lower() != 'usd':
                            raise DashboardError('Only USD organization costs are supported.')
                        cost = float(amount.get('value') or 0)
                        if not math.isfinite(cost):
                            raise ValueError
                        add(daily, day, cost)
                        add(projects, row.get('project_id') or 'unassigned', cost)
                        add(line_items, row.get('line_item') or 'unassigned', cost)
                for bucket in official_buckets('usage/completions', key, start_ts, end_ts, ['model', 'project_id']):
                    day = timestamp(bucket['start_time']).date().isoformat()
                    if not start <= day < end_day:
                        continue
                    for row in bucket['results']:
                        usage_record(dict(model=row.get('model') or 'unknown', **tokens(dict(
                            input_tokens=row.get('input_tokens', 0),
                            cached_input_tokens=row.get('input_cached_tokens', 0),
                            output_tokens=row.get('output_tokens', 0)))))
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                raise DashboardError('OpenAI API returned an unexpected cost or usage record.') from None
        warnings.add('Official totals are organization costs in this date range, not a live gift-credit balance. Billing data may lag.')
        warnings.add('Model costs are token-based estimates from completions usage; they may not reconcile with official costs.')
    else:
        for row in local_records(config, warnings):
            if start <= row['day'] < end_day:
                cost = usage_record(row)
                add(daily, row['day'], cost)
                add(projects, row['project'], cost)
        warnings.add('Local costs are API-price estimates, not actual billing. Subscription sessions may not consume API credit.')
    if fallback:
        warnings.add('Some models use the default pricing fallback; edit pricing.json for accurate estimates.')
    days = []
    cursor = dt.date.fromisoformat(start)
    while cursor.isoformat() < end_day:
        day = cursor.isoformat()
        days.append(dict(date=day, cost=round(daily.get(day, 0), 6)))
        cursor += dt.timedelta(days=1)

    def breakdown(values, estimated):
        return [dict(name=k, cost=round(v, 6), estimated=estimated)
                for k, v in sorted(values.items(), key=lambda item: (-item[1], item[0]))]

    return dict(app='openai-credit-dashboard', mode=mode, estimated=mode == 'local',
                currency='USD', timezone='UTC', updated_at=now.isoformat(),
                grant_total=config['grant_total'], grant_start=start, expiry=config['expiry'],
                **projection(daily, config['grant_total'], start, config['expiry'], now),
                daily=days, by_model=breakdown(models, True),
                by_project=breakdown(projects, mode == 'local'),
                by_line_item=breakdown(line_items, False), tokens=token_total,
                top_days=sorted(days, key=lambda row: (-row['cost'], row['date']))[:10],
                fallback_models=sorted(fallback), warnings=sorted(warnings))
