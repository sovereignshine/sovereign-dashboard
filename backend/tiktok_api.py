"""
TikTok for Business API client — organic video stats for Sovereign Dashboard.

Setup:
  1. Create a TikTok Developer app at https://developers.tiktok.com/
  2. Add "Login Kit" and "Video Kit" products
  3. Set redirect URI to http://localhost:5001/api/organic/tiktok/callback
  4. Copy Client Key + Client Secret into .env

OAuth flow:
  GET /api/organic/tiktok/auth  → redirects user to TikTok login
  GET /api/organic/tiktok/callback?code=xxx  → exchanges code, saves token
"""

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TIKTOK_AUTH_URL  = 'https://www.tiktok.com/v2/auth/authorize/'
TIKTOK_TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/'
TIKTOK_VIDEO_URL = 'https://open.tiktokapis.com/v2/video/list/'

VIDEO_FIELDS = (
    'id,title,create_time,cover_image_url,share_url,'
    'video_description,duration,statistics'
)

TOKEN_FILE = Path(__file__).parent / 'tiktok_token.json'


class TikTokAPI:
    def __init__(self, client_key, client_secret, redirect_uri):
        self.client_key    = client_key
        self.client_secret = client_secret
        self.redirect_uri  = redirect_uri
        self._token_data   = self._load_token()

    # ─── Token management ────────────────────────────────────────────────────

    def _load_token(self):
        if TOKEN_FILE.exists():
            try:
                with open(TOKEN_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _save_token(self, data):
        self._token_data = data
        with open(TOKEN_FILE, 'w') as f:
            json.dump(data, f)

    def is_connected(self):
        if not self._token_data:
            return False
        return time.time() < self._token_data.get('expires_at', 0) - 300

    def _access_token(self):
        if not self.is_connected():
            if self._token_data and self._token_data.get('refresh_token'):
                if not self._refresh():
                    raise RuntimeError('TikTok token expired and refresh failed')
            else:
                raise RuntimeError('TikTok account not connected')
        return self._token_data['access_token']

    def _refresh(self):
        try:
            r = requests.post(TIKTOK_TOKEN_URL, data={
                'client_key':    self.client_key,
                'client_secret': self.client_secret,
                'grant_type':    'refresh_token',
                'refresh_token': self._token_data['refresh_token'],
            }, timeout=30)
            r.raise_for_status()
            data = r.json().get('data', {})
            data['expires_at'] = time.time() + data.get('expires_in', 86400)
            self._save_token(data)
            return True
        except (requests.HTTPError, KeyError):
            return False

    # ─── OAuth helpers ───────────────────────────────────────────────────────

    def get_auth_url(self):
        state  = secrets.token_urlsafe(16)
        params = {
            'client_key':    self.client_key,
            'scope':         'user.info.basic,video.list',
            'response_type': 'code',
            'redirect_uri':  self.redirect_uri,
            'state':         state,
        }
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        return f'{TIKTOK_AUTH_URL}?{qs}', state

    def exchange_code(self, code):
        r = requests.post(TIKTOK_TOKEN_URL, data={
            'client_key':    self.client_key,
            'client_secret': self.client_secret,
            'code':          code,
            'grant_type':    'authorization_code',
            'redirect_uri':  self.redirect_uri,
        }, timeout=30)
        r.raise_for_status()
        data = r.json().get('data', {})
        data['expires_at'] = time.time() + data.get('expires_in', 86400)
        self._save_token(data)
        return data

    def disconnect(self):
        self._token_data = None
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()

    # ─── Video data ───────────────────────────────────────────────────────────

    def get_videos(self, max_count=50):
        token = self._access_token()
        r = requests.post(
            TIKTOK_VIDEO_URL,
            headers={'Authorization': f'Bearer {token}'},
            params={'fields': VIDEO_FIELDS},
            json={'max_count': max_count},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get('data', {}).get('videos', [])

    # ─── Aggregated stats (dashboard-ready) ──────────────────────────────────

    def get_stats(self, days=30):
        cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
        videos  = self.get_videos()

        recent = []
        for v in videos:
            ts = v.get('create_time', 0)
            if ts and datetime.fromtimestamp(ts, tz=timezone.utc) >= cutoff:
                recent.append(v)

        total_plays    = 0
        total_comments = 0
        total_likes    = 0
        total_shares   = 0
        total_saves    = 0
        enriched       = []

        for v in recent:
            stats    = v.get('statistics', {})
            plays    = stats.get('play_count',    0)
            comments = stats.get('comment_count', 0)
            likes    = stats.get('digg_count',    0)
            shares   = stats.get('share_count',   0)
            saves    = stats.get('collect_count', 0)

            total_plays    += plays
            total_comments += comments
            total_likes    += likes
            total_shares   += shares
            total_saves    += saves

            ts = v.get('create_time', 0)
            try:
                date_label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%b %-d')
            except (OSError, ValueError):
                date_label = ''

            title = (v.get('title') or v.get('video_description') or '').strip()

            enriched.append({
                'id':        v['id'],
                'hook':      title[:100] or '(no title)',
                'type':      'Video',
                'permalink': v.get('share_url', ''),
                'date':      date_label,
                'comments':  comments,
                'likes':     likes,
                'views':     _fmt_count(plays),
                'views_raw': plays,
                'saves':     saves,
                'shares':    shares,
                'reach':     plays,
            })

        enriched.sort(key=lambda v: v['comments'], reverse=True)

        return {
            'platform': 'tiktok',
            'kpis': {
                'total_comments': total_comments,
                'total_likes':    total_likes,
                'total_views':    total_plays,
                'total_saves':    total_saves,
                'total_shares':   total_shares,
                'post_count':     len(recent),
                'followers':      0,  # requires separate user.info.basic scope call
            },
            'posts': enriched[:20],
        }

    def get_account_info(self):
        token = self._access_token()
        r = requests.get(
            'https://open.tiktokapis.com/v2/user/info/',
            headers={'Authorization': f'Bearer {token}'},
            params={'fields': 'display_name,avatar_url,follower_count,following_count,video_count'},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get('data', {}).get('user', {})
        return {
            'username':  data.get('display_name', ''),
            'followers': data.get('follower_count', 0),
            'videos':    data.get('video_count', 0),
            'avatar':    data.get('avatar_url', ''),
        }


def _fmt_count(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}K'
    return str(n)
