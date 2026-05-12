import requests

GRAPH_VERSION = 'v21.0'
BASE_URL = f'https://graph.facebook.com/{GRAPH_VERSION}'

# All fields we request from the insights endpoint
INSIGHT_FIELDS = ','.join([
    'spend',
    'impressions',
    'reach',
    'clicks',
    'ctr',
    'frequency',
    'actions',
    'cost_per_action_type',
    'purchase_roas',
    'cpp',
    'date_start',
    'date_stop',
])

# How each dashboard preset maps to Meta API params
PRESET_MAP = {
    'today':        {'date_preset': 'today'},
    'yesterday':    {'date_preset': 'yesterday'},
    'last_7_days':  {'date_preset': 'last_7d'},
    'last_14_days': {'date_preset': 'last_14d'},
    'last_30_days': {'date_preset': 'last_30d'},
    # legacy / internal
    'live':        {'date_preset': 'today',       'time_increment': 'hourly'},
    'hourly':      {'date_preset': 'today',       'time_increment': 'hourly'},
    '4hr':         {'date_preset': 'today',       'time_increment': 'hourly'},
    'daily':       {'date_preset': 'last_7d',     'time_increment': '1'},
    'weekly':      {'date_preset': 'last_28d',    'time_increment': '7'},
    'this_month':  {'date_preset': 'this_month'},
    'last_month':  {'date_preset': 'last_month'},
    'last_90_days':{'date_preset': 'last_90d'},
    'last_year':   {'date_preset': 'last_year'},
}

# Priority-ordered lead action types — first match wins, avoiding double-counts.
# Lead form campaigns report both 'lead' and 'lead_grouped' for the same event;
# messaging campaigns report conversation starts instead of form leads.
LEAD_ACTION_TYPES = [
    'lead',                                              # lead form completions (primary)
    'onsite_conversion.lead',                            # onsite lead variant
    'onsite_web_lead',                                   # website lead pixel
    'leadgen_grouped',                                   # legacy lead gen
    'onsite_conversion.lead_grouped',                    # grouped (same count as 'lead', skip if lead matched)
    'onsite_conversion.messaging_conversation_started_7d', # messaging/DM campaigns
]
LEAD_ACTION_SET = set(LEAD_ACTION_TYPES)  # for fast lookup in cost parsing


class MetaAPI:
    def __init__(self, access_token):
        self.token = access_token

    # ─── Internal ────────────────────────────────────────────────────────────

    def _get(self, path, params=None):
        params = params or {}
        params['access_token'] = self.token
        r = requests.get(f'{BASE_URL}/{path}', params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _paginate(self, path, params=None):
        """Follow Meta's cursor-based pagination and return all records."""
        results = []
        data = self._get(path, params)
        results.extend(data.get('data', []))
        while True:
            next_url = data.get('paging', {}).get('next')
            if not next_url:
                break
            r = requests.get(next_url, timeout=30)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get('data', []))
        return results

    # ─── Accounts ────────────────────────────────────────────────────────────

    def get_accounts(self):
        """List every ad account the System User token can access."""
        rows = self._paginate('me/adaccounts', {
            'fields': 'id,name,account_id,currency,account_status,amount_spent',
            'limit':  200,
        })
        return [
            {
                'id':         row['id'],                       # act_XXXXX
                'account_id': row.get('account_id'),           # raw numeric
                'name':       row.get('name'),
                'currency':   row.get('currency', 'USD'),
                'status':     row.get('account_status'),       # 1=active
                'business':   row.get('business', {}).get('name'),
                'spent_all_time': row.get('amount_spent'),
            }
            for row in rows
        ]

    # ─── Insights ─────────────────────────────────────────────────────────────

    def get_insights(self, account_id, preset='last_30_days'):
        """
        Fetch ad insights for a single account.
        Returns a flat metrics dict for single-period presets,
        or {series: [...], summary: {...}} for time-series presets.
        """
        api_params = {**PRESET_MAP.get(preset, {'date_preset': preset})}
        api_params['fields'] = INSIGHT_FIELDS
        api_params['limit']  = 50

        try:
            rows = self._paginate(f'{account_id}/insights', api_params)
        except requests.HTTPError as e:
            return {'error': str(e), 'account_id': account_id}

        if not rows:
            return self._empty()

        if preset == 'live':
            # Most recent completed hourly bucket
            return self._parse_row(rows[-1])

        if preset == '4hr':
            rows = rows[-4:] if len(rows) >= 4 else rows
            series = [self._parse_row(r) for r in rows]
            return {'series': series, 'summary': self._summarize(series)}

        if len(rows) > 1:
            series = [self._parse_row(r) for r in rows]
            return {'series': series, 'summary': self._summarize(series)}

        return self._parse_row(rows[0])

    def _parse_row(self, row):
        leads   = self._extract_actions(row.get('actions', []))
        cpl_raw = self._extract_cost(row.get('cost_per_action_type', []))
        roas_list = row.get('purchase_roas', [])
        roas = float(roas_list[0]['value']) if roas_list else 0.0

        return {
            'spend':      round(float(row.get('spend', 0)), 2),
            'impressions':int(row.get('impressions', 0)),
            'reach':      int(row.get('reach', 0)),
            'clicks':     int(row.get('clicks', 0)),
            'ctr':        round(float(row.get('ctr', 0)), 2),
            'frequency':  round(float(row.get('frequency', 0)), 2),
            'leads':      leads,
            'cpl':        round(cpl_raw, 2) if cpl_raw is not None else None,
            'roas':       round(roas, 2),
            'date_start': row.get('date_start'),
            'date_stop':  row.get('date_stop'),
        }

    def _summarize(self, series):
        if not series:
            return self._empty()
        n = len(series)
        return {
            'spend':       round(sum(r.get('spend', 0)      for r in series), 2),
            'impressions': sum(r.get('impressions', 0)       for r in series),
            'reach':       max(r.get('reach', 0)             for r in series),
            'clicks':      sum(r.get('clicks', 0)            for r in series),
            'leads':       sum(r.get('leads', 0)             for r in series),
            'ctr':         round(sum(r.get('ctr', 0)         for r in series) / n, 2),
            'frequency':   round(sum(r.get('frequency', 0)   for r in series) / n, 2),
            'roas':        round(sum(r.get('roas', 0)        for r in series) / n, 2),
        }

    def _extract_actions(self, actions):
        # Build a lookup so we can walk priority list and take first match
        by_type = {a['action_type']: int(float(a.get('value', 0))) for a in actions}
        for action_type in LEAD_ACTION_TYPES:
            if action_type in by_type:
                return by_type[action_type]
        return 0

    def _extract_cost(self, costs):
        by_type = {c['action_type']: float(c.get('value', 0)) for c in costs}
        for action_type in LEAD_ACTION_TYPES:
            if action_type in by_type:
                return by_type[action_type]
        return None

    def _empty(self):
        return {
            'spend': 0, 'impressions': 0, 'reach': 0,
            'clicks': 0, 'leads': 0, 'ctr': 0.0,
            'frequency': 0.0, 'roas': 0.0, 'cpl': None,
        }

    # ─── Budget ──────────────────────────────────────────────────────────────

    def get_total_daily_budget(self, account_id):
        """Sum daily_budget across campaigns (or ad sets if campaign-level is 0).
        Meta returns budgets in cents — divide by 100 for dollars."""
        try:
            # Try campaign-level first (CBO)
            campaigns = self._paginate(f'{account_id}/campaigns', {
                'fields': 'daily_budget,status',
                'limit':  50,
            })
            total = sum(int(c['daily_budget']) for c in campaigns if c.get('daily_budget'))

            # Fallback to ad set-level (ABO) if campaigns have no budget set
            if total == 0:
                adsets = self._paginate(f'{account_id}/adsets', {
                    'fields': 'daily_budget,status',
                    'limit':  50,
                })
                total = sum(int(a['daily_budget']) for a in adsets if a.get('daily_budget'))

            return round(total / 100, 2)
        except Exception:
            return 0

    # ─── Campaigns ───────────────────────────────────────────────────────────

    def get_campaigns(self, account_id):
        """Active + paused campaigns for a single account, with basic insights."""
        campaigns = self._paginate(f'{account_id}/campaigns', {
            'fields':           'id,name,status,objective,daily_budget,lifetime_budget',
            'effective_status': '["ACTIVE","PAUSED","LEARNING"]',
            'limit':            50,
        })

        # Attach last-30-day spend + leads per campaign
        enriched = []
        for c in campaigns:
            try:
                ins = self._paginate(f'{c["id"]}/insights', {
                    'fields':      'spend,actions,cost_per_action_type,impressions,ctr,reach',
                    'date_preset': 'last_30d',
                    'limit':       1,
                })
                metrics = self._parse_row(ins[0]) if ins else self._empty()
            except Exception:
                metrics = self._empty()

            enriched.append({
                'id':               c['id'],
                'name':             c.get('name'),
                'status':           c.get('status'),
                'objective':        c.get('objective'),
                'daily_budget':     c.get('daily_budget'),
                'lifetime_budget':  c.get('lifetime_budget'),
                **metrics,
            })

        return enriched
