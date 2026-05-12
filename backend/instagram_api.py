"""
Instagram Graph API client — organic content stats for Sovereign Dashboard.
Requires a Meta System User token with:
  instagram_basic, instagram_manage_insights, pages_read_engagement
"""

import requests
from datetime import datetime, timedelta, timezone

GRAPH = 'https://graph.facebook.com/v21.0'

# Metrics available per media type
_MEDIA_METRICS = {
    'IMAGE':          'impressions,reach,saved,likes,comments,shares',
    'CAROUSEL_ALBUM': 'impressions,reach,saved,likes,comments,shares',
    'VIDEO':          'impressions,reach,saved,likes,comments,shares,video_views,plays',
    'REEL':           'comments,likes,plays,reach,saved,shares,total_interactions',
}


class InstagramAPI:
    def __init__(self, access_token):
        self.token = access_token

    # ─── Internal ────────────────────────────────────────────────────────────

    def _get(self, path, **params):
        params['access_token'] = self.token
        r = requests.get(f'{GRAPH}/{path}', params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _paginate(self, path, **params):
        params['access_token'] = self.token
        r = requests.get(f'{GRAPH}/{path}', params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        results = list(data.get('data', []))
        while True:
            next_url = data.get('paging', {}).get('next')
            if not next_url:
                break
            r = requests.get(next_url, timeout=30)
            r.raise_for_status()
            data = r.json()
            results.extend(data.get('data', []))
        return results

    # ─── Discovery ────────────────────────────────────────────────────────────

    def discover_accounts(self):
        """Find Instagram Business accounts linked to Facebook Pages via the token."""
        try:
            pages = self._paginate(
                'me/accounts',
                fields='id,name,instagram_business_account',
                limit=100,
            )
        except requests.HTTPError:
            return []

        accounts = []
        for page in pages:
            ig = page.get('instagram_business_account')
            if not ig:
                continue
            try:
                info = self._get(ig['id'], fields='id,username,followers_count,media_count')
                accounts.append({
                    'ig_user_id':  info['id'],
                    'username':    info.get('username', ''),
                    'followers':   info.get('followers_count', 0),
                    'media_count': info.get('media_count', 0),
                    'page_name':   page.get('name', ''),
                    'label':       f"@{info.get('username', info['id'])}",
                })
            except requests.HTTPError:
                pass
        return accounts

    # ─── Account info ────────────────────────────────────────────────────────

    def get_account(self, ig_user_id):
        return self._get(
            ig_user_id,
            fields='id,username,name,followers_count,media_count,biography,profile_picture_url',
        )

    # ─── Media ───────────────────────────────────────────────────────────────

    def get_media(self, ig_user_id, limit=100):
        return self._paginate(
            f'{ig_user_id}/media',
            fields='id,caption,media_type,timestamp,like_count,comments_count,thumbnail_url,permalink',
            limit=limit,
        )

    def get_media_insights(self, media_id, media_type='IMAGE'):
        metrics = _MEDIA_METRICS.get(media_type, _MEDIA_METRICS['IMAGE'])
        try:
            data = self._get(f'{media_id}/insights', metric=metrics)
            result = {}
            for item in data.get('data', []):
                val = item.get('value')
                if val is None:
                    vals = item.get('values', [])
                    val = vals[0].get('value', 0) if vals else 0
                result[item['name']] = val
            return result
        except requests.HTTPError:
            return {}

    # ─── Account-level insights ──────────────────────────────────────────────

    def get_account_insights(self, ig_user_id, days=30):
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=days)
        try:
            data = self._get(
                f'{ig_user_id}/insights',
                metric='reach,impressions,profile_views',
                period='day',
                since=int(since.timestamp()),
                until=int(until.timestamp()),
            )
            totals = {}
            for metric_obj in data.get('data', []):
                name  = metric_obj['name']
                total = sum(v.get('value', 0) for v in metric_obj.get('values', []))
                totals[name] = total
            return totals
        except requests.HTTPError:
            return {}

    # ─── Aggregated stats (dashboard-ready) ──────────────────────────────────

    def get_stats(self, ig_user_id, days=30):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        account    = self.get_account(ig_user_id)
        media_list = self.get_media(ig_user_id)

        # Keep only posts within the requested window
        recent = []
        for m in media_list:
            ts_str = m.get('timestamp', '')
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts >= cutoff:
                    recent.append(m)
            except (ValueError, TypeError):
                pass

        total_comments = sum(m.get('comments_count', 0) for m in recent)
        total_likes    = sum(m.get('like_count',      0) for m in recent)

        # Fetch per-post insights for top posts (capped to avoid rate limits)
        top_by_comments = sorted(recent, key=lambda m: m.get('comments_count', 0), reverse=True)[:20]
        total_views = total_saves = total_shares = 0
        enriched_posts = []

        for m in top_by_comments:
            media_type = m.get('media_type', 'IMAGE')
            insights   = self.get_media_insights(m['id'], media_type)

            views  = insights.get('plays', insights.get('video_views', insights.get('impressions', 0)))
            saves  = insights.get('saved', 0)
            shares = insights.get('shares', 0)
            reach  = insights.get('reach', 0)

            total_views  += views
            total_saves  += saves
            total_shares += shares

            caption  = (m.get('caption') or '').strip()
            hook     = caption[:100].split('\n')[0].strip() or '(no caption)'
            ts_str   = m.get('timestamp', '')
            try:
                ts         = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                date_label = ts.strftime('%b %-d')
            except (ValueError, TypeError):
                date_label = ''

            display_type = {'REEL': 'Reel', 'VIDEO': 'Video', 'CAROUSEL_ALBUM': 'Carousel'}.get(media_type, 'Post')

            enriched_posts.append({
                'id':        m['id'],
                'hook':      hook,
                'type':      display_type,
                'permalink': m.get('permalink', ''),
                'date':      date_label,
                'comments':  m.get('comments_count', 0),
                'likes':     m.get('like_count', 0),
                'views':     _fmt_count(views),
                'views_raw': views,
                'saves':     saves,
                'shares':    shares,
                'reach':     reach,
            })

        acct_insights = self.get_account_insights(ig_user_id, days=days)

        return {
            'platform': 'instagram',
            'account': {
                'id':                  account.get('id'),
                'username':            account.get('username', ''),
                'name':                account.get('name', ''),
                'followers':           account.get('followers_count', 0),
                'media_count':         account.get('media_count', 0),
                'profile_picture_url': account.get('profile_picture_url', ''),
            },
            'kpis': {
                'total_comments':  total_comments,
                'total_likes':     total_likes,
                'total_views':     total_views,
                'total_saves':     total_saves,
                'total_shares':    total_shares,
                'post_count':      len(recent),
                'followers':       account.get('followers_count', 0),
                'reach':           acct_insights.get('reach', 0),
                'impressions':     acct_insights.get('impressions', 0),
                'profile_views':   acct_insights.get('profile_views', 0),
            },
            'posts': enriched_posts,
        }


def _fmt_count(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}K'
    return str(n)
