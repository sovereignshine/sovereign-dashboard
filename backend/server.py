"""
Sovereign Dashboard — Meta Marketing API + Organic (Instagram / TikTok) backend
Run: python3 server.py
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

from cache import Cache
from competitor_api import CompetitorAPI
from instagram_api import InstagramAPI
from meta_api import MetaAPI
from tiktok_api import TikTokAPI

load_dotenv()

FRONTEND_DIR = Path(__file__).parent.parent  # project root — index.html lives here

app  = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path='')
CORS(app)


# ─── Frontend routes ───────────────────────────────────────────────────────────

@app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/client')
def serve_client():
    return send_from_directory(FRONTEND_DIR, 'client.html')

_token = os.getenv('META_ACCESS_TOKEN', '')
api    = MetaAPI(_token)
ig     = InstagramAPI(_token)
comp   = CompetitorAPI(_token)
cache  = Cache()

_tt_key    = os.getenv('TIKTOK_CLIENT_KEY', '')
_tt_secret = os.getenv('TIKTOK_CLIENT_SECRET', '')
_tt_redir  = os.getenv('TIKTOK_REDIRECT_URI', 'http://localhost:5001/api/organic/tiktok/callback')
tt = TikTokAPI(_tt_key, _tt_secret, _tt_redir)

CLIENTS_FILE        = Path(__file__).parent / 'clients.json'
ORGANIC_CONFIG_FILE = Path(__file__).parent / 'organic_config.json'
DASHBOARD_URL       = os.getenv('DASHBOARD_URL', 'http://localhost:3000')

# Accounts excluded from the client-facing portfolio view
EXCLUDED_ACCOUNTS = {
    'act_2065213980665816',  # Sovereign Shine Detail — owner account
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_clients():
    with open(CLIENTS_FILE) as f:
        return json.load(f)

def load_organic_config():
    with open(ORGANIC_CONFIG_FILE) as f:
        return json.load(f)

def err(msg, status=500):
    return jsonify({'error': msg}), status

def cached_or_fetch(key, fetch_fn, preset='last_30_days'):
    hit = cache.get(key)
    if hit is not None:
        return jsonify({**hit, '_cached': True})
    data = fetch_fn()
    cache.set(key, data, ttl=cache.ttl_for(preset))
    return jsonify({**data, '_cached': False}) if isinstance(data, dict) else jsonify(data)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    """Quick liveness check."""
    return jsonify({'status': 'ok', 'token_set': bool(_token)})


@app.route('/api/accounts')
def list_accounts():
    """
    Discover every ad account the System User token can see.
    Use this once to grab account IDs for clients.json.
    """
    cached = cache.get('accounts')
    if cached:
        return jsonify(cached)
    data = api.get_accounts()
    cache.set('accounts', data, ttl=3600)
    return jsonify(data)


@app.route('/api/portfolio')
def portfolio():
    """
    Auto-discovers every active ad account the token can see, then
    merges live Meta insights. clients.json is used only for metadata
    overrides (name, city, color, niche, budget) — never as the source
    of account IDs.
    ?preset=live|hourly|4hr|daily|weekly|this_month|last_30_days|last_90_days|last_year
    """
    preset    = request.args.get('preset', 'last_30_days')
    cache_key = f'portfolio:{preset}'
    cached    = cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    # 1. Discover all ad accounts the token can reach
    try:
        discovered = api.get_accounts()
    except Exception as e:
        return err(f'Could not discover ad accounts: {e}')

    # 2. Load clients.json for metadata overrides (name, color, city…)
    meta_by_id = {}
    try:
        for c in load_clients():
            aid = c.get('account_id', '')
            if aid and 'REPLACE' not in aid:
                meta_by_id[aid] = c
    except Exception:
        pass

    COLORS = ['#2563eb','#f59e0b','#8b5cf6','#10b981','#ec4899',
              '#3b82f6','#14b8a6','#f97316','#6366f1','#ef4444']

    result = []
    for i, acct in enumerate(discovered):
        acct_id = acct['id']          # e.g. act_XXXXXXXX
        if acct_id in EXCLUDED_ACCOUNTS:
            continue
        meta    = meta_by_id.get(acct_id, {})

        daily_budget = api.get_total_daily_budget(acct_id)
        budget_str   = f'${daily_budget:,.0f}' if daily_budget else '$0'

        base = {
            'id':          meta.get('id', i + 1),
            'account_id':  acct_id,
            'name':        meta.get('name') or acct.get('name', f'Account {i + 1}'),
            'city':        meta.get('city', ''),
            'niche':       meta.get('niche', 'Detail'),
            'color':       meta.get('color', COLORS[i % len(COLORS)]),
            'dailyBudget': budget_str,
        }

        insights = api.get_insights(acct_id, preset)
        if 'summary' in insights:
            merged = {**base, **insights['summary'], 'series': insights['series']}
        else:
            merged = {**base, **insights}

        # Derive adStatus from spend so the frontend status badge always has a value
        spend = merged.get('spend', 0) or 0
        merged['adStatus'] = 'Active' if spend > 0 else 'Paused'

        result.append(merged)

    cache.set(cache_key, result, ttl=cache.ttl_for(preset))
    return jsonify(result)


@app.route('/api/client/<path:account_id>/insights')
def client_insights(account_id):
    """
    Single-account insights.
    ?preset=live|hourly|4hr|daily|weekly|this_month|last_30_days|last_90_days|last_year
    """
    preset    = request.args.get('preset', 'last_30_days')
    cache_key = f'insights:{account_id}:{preset}'
    cached    = cache.get(cache_key)
    if cached is not None:
        return jsonify({**cached, '_cached': True})

    data = api.get_insights(account_id, preset)
    cache.set(cache_key, data, ttl=cache.ttl_for(preset))
    return jsonify({**data, '_cached': False})


@app.route('/api/client/<path:account_id>/campaigns')
def client_campaigns(account_id):
    """Campaign list + 30-day metrics for a single account."""
    cache_key = f'campaigns:{account_id}'
    cached    = cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    data = api.get_campaigns(account_id)
    cache.set(cache_key, data, ttl=1800)  # 30 min
    return jsonify(data)


@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    """Force-clear all cached data (useful after manual BM changes)."""
    cache.clear()
    return jsonify({'cleared': True})


# ─── Organic — shared ─────────────────────────────────────────────────────────

@app.route('/api/organic/accounts')
def organic_accounts():
    """Return configured Instagram accounts and TikTok connection status."""
    cfg = load_organic_config()
    return jsonify({
        'instagram': cfg.get('instagram', []),
        'tiktok': {
            'connected': tt.is_connected(),
            'configured': bool(_tt_key and _tt_secret),
        },
    })


# ─── Organic — Instagram ──────────────────────────────────────────────────────

@app.route('/api/organic/instagram/discover')
def ig_discover():
    """Find all Instagram Business accounts linked to the Meta token's Pages."""
    cache_key = 'ig:discover'
    cached = cache.get(cache_key)
    if cached:
        return jsonify(cached)
    accounts = ig.discover_accounts()
    cache.set(cache_key, accounts, ttl=3600)
    return jsonify(accounts)


@app.route('/api/organic/instagram/stats')
def ig_stats():
    """
    Aggregate organic stats for one Instagram account.
    ?ig_user_id=xxx&days=30
    """
    ig_user_id = request.args.get('ig_user_id', '')
    days       = int(request.args.get('days', 30))

    if not ig_user_id or 'REPLACE' in ig_user_id:
        return err('ig_user_id not configured — run /api/organic/instagram/discover first', 400)

    cache_key = f'ig:stats:{ig_user_id}:{days}'
    cached    = cache.get(cache_key)
    if cached:
        return jsonify({**cached, '_cached': True})

    try:
        data = ig.get_stats(ig_user_id, days=days)
    except Exception as e:
        return err(str(e))

    cache.set(cache_key, data, ttl=300)
    return jsonify({**data, '_cached': False})


# ─── Organic — TikTok ─────────────────────────────────────────────────────────

@app.route('/api/organic/tiktok/auth')
def tt_auth():
    """Start TikTok OAuth — redirects browser to TikTok login page."""
    if not _tt_key:
        return err('TIKTOK_CLIENT_KEY not set in .env', 400)
    auth_url, _ = tt.get_auth_url()
    return redirect(auth_url)


@app.route('/api/organic/tiktok/callback')
def tt_callback():
    """TikTok OAuth callback — exchange code for tokens then redirect to dashboard."""
    code  = request.args.get('code')
    error = request.args.get('error')

    if error or not code:
        return redirect(f'{DASHBOARD_URL}?tiktok=error&reason={error or "no_code"}')

    try:
        tt.exchange_code(code)
        return redirect(f'{DASHBOARD_URL}?tiktok=connected')
    except Exception as e:
        return redirect(f'{DASHBOARD_URL}?tiktok=error&reason={str(e)}')


@app.route('/api/organic/tiktok/disconnect', methods=['POST'])
def tt_disconnect():
    tt.disconnect()
    return jsonify({'disconnected': True})


@app.route('/api/organic/tiktok/stats')
def tt_stats():
    """
    Aggregate organic stats for the connected TikTok account.
    ?days=30
    """
    if not tt.is_connected():
        return err('TikTok not connected — visit /api/organic/tiktok/auth', 401)

    days      = int(request.args.get('days', 30))
    cache_key = f'tt:stats:{days}'
    cached    = cache.get(cache_key)
    if cached:
        return jsonify({**cached, '_cached': True})

    try:
        data = tt.get_stats(days=days)
    except Exception as e:
        return err(str(e))

    cache.set(cache_key, data, ttl=300)
    return jsonify({**data, '_cached': False})


# ─── Competitor Intelligence ──────────────────────────────────────────────────

@app.route('/api/competitors')
def competitors():
    """
    Meta Ad Library — B2C competitor ads by service.
    ?service=ppf|ceramic|tinting|detailing
    Only returns ads running 14+ days. Cached 2 hours.
    """
    service = request.args.get('service', 'ppf')
    if service not in ('ppf', 'ceramic', 'tinting', 'detailing'):
        return err('service must be ppf, ceramic, tinting, or detailing', 400)

    cache_key = f'competitors:{service}'
    force = request.args.get('force')
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return jsonify({'ads': cached, '_cached': True})

    try:
        ads = comp.get_service_ads(service)
    except Exception as e:
        return err(str(e))

    cache.set(cache_key, ads, ttl=7200)  # 2 hr
    return jsonify({'ads': ads, '_cached': False})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not _token:
        print('\n⚠️  META_ACCESS_TOKEN not set. Copy .env.example → .env and add your token.\n')
    else:
        print(f'\n✓  Token loaded ({_token[:12]}...)')

    print('✓  Starting Sovereign backend on http://localhost:5001\n')
    app.run(debug=True, port=5001, use_reloader=False)
