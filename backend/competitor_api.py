from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import requests
import time

APIFY_TOKEN = 'apify_api_pBQ4NxggwsgKUD9ouTd98pFRtElCha0Br7SE'
ACTOR_ID    = 'whoareyouanas~meta-ad-scraper'
RUNS_URL    = f'https://api.apify.com/v2/acts/{ACTOR_ID}/runs'

MIN_DAYS      = 5
MAX_DAYS      = 90
POLL_INTERVAL = 5    # seconds between status checks
POLL_TIMEOUT  = 210  # max seconds to wait for a single run

# CTAs that signal e-commerce/retail — not lead gen, exclude these
ECOMMERCE_CTAS = {
    'shop now', 'order now', 'buy now', 'add to cart', 'download',
    'watch more', 'see menu', 'get app', 'play game', 'install now',
    'use app', 'open link', 'get directions', 'shop', 'buy',
    'view product', 'see offer', 'get deal',
}

SERVICE_KEYWORDS = {
    'ppf': [
        'paint protection film installation near me',
        'PPF installer near me',
        'clear bra installation service',
        'paint protection film shop',
        'XPEL certified installer',
    ],
    'ceramic': [
        'ceramic coating service near me',
        'ceramic coating shop near me',
        'get ceramic coating near me',
        'professional ceramic coating installer',
        'ceramic coating appointment',
    ],
    'tinting': [
        'window tinting shop near me',
        'car window tinting service near me',
        'auto tint shop near me',
        'window tinting appointment',
        'window tinting installer near me',
    ],
    'detailing': [
        'mobile detailing service near me',
        'auto detailing shop near me',
        'mobile car detailer near me',
        'car detailing appointment',
        'professional detailing service near me',
    ],
}


class CompetitorAPI:
    def __init__(self, _access_token=None):
        pass

    def _start_run(self, keyword):
        payload = {
            'searchQuery':  keyword,
            'country':      'US',
            'activeStatus': 'active',
            'adType':       'all',
            'mediaType':    'all',
            'sortMode':     'total_impressions',
            'sortDirection':'desc',
        }
        r = requests.post(
            f'{RUNS_URL}?token={APIFY_TOKEN}&maxItems=10',
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get('data', {})
        return data.get('id'), data.get('defaultDatasetId')

    def _wait_for_run(self, run_id):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            r = requests.get(
                f'https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}',
                timeout=10,
            )
            status = r.json().get('data', {}).get('status')
            if status == 'SUCCEEDED':
                return True
            if status in ('FAILED', 'TIMED-OUT', 'ABORTED'):
                print(f'[competitor_api] run {run_id} ended with {status}')
                return False
            time.sleep(POLL_INTERVAL)
        print(f'[competitor_api] run {run_id} exceeded poll timeout')
        return False

    def _fetch_dataset(self, dataset_id):
        r = requests.get(
            f'https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_TOKEN}&limit=10',
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def _run_keyword(self, keyword):
        try:
            run_id, dataset_id = self._start_run(keyword)
            if not run_id:
                return []
            if not self._wait_for_run(run_id):
                return []
            return self._fetch_dataset(dataset_id)
        except Exception as e:
            print(f'[competitor_api] keyword "{keyword}" failed: {e}')
            return []

    def _days_running(self, ad):
        start = ad.get('startDate')
        if not start:
            return None
        try:
            if isinstance(start, (int, float)):
                dt = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(start).replace('Z', '+00:00'))
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return None

    def _normalize(self, ad):
        platforms = ad.get('platforms') or []
        images = ad.get('images') or []
        videos = ad.get('videos') or []
        return {
            'id':                ad.get('libraryID'),
            'page_name':         ad.get('brand'),
            'brand_logo':        ad.get('brandLogo'),
            'body':              ad.get('body'),
            'title':             ad.get('linkTitle'),
            'description':       ad.get('linkDescription'),
            'snapshot_url':      ad.get('linkUrl'),
            'platforms':         platforms if isinstance(platforms, list) else [platforms],
            'started':           ad.get('startDate'),
            'days_running':      self._days_running(ad),
            'cta_text':          ad.get('ctaText'),
            'cta_url':           ad.get('ctaUrl'),
            'format':            ad.get('format'),
            'image_urls':        [img['url'] for img in images if img.get('url')],
            'video_url':         videos[0]['url'] if videos and videos[0].get('url') else None,
            'video_duration':    videos[0].get('duration') if videos else None,
            'similar_ad_count':  ad.get('similarAdCount'),
        }

    def _collect(self, keywords, total_limit=40):
        results, seen = [], set()

        with ThreadPoolExecutor(max_workers=len(keywords)) as pool:
            futures = {pool.submit(self._run_keyword, kw): kw for kw in keywords}
            for future in as_completed(futures):
                for ad in future.result():
                    ad_id = (ad.get('libraryID') or ad.get('id')
                             or ad.get('brand', '') + str(ad.get('startDate', '')))
                    if ad_id and ad_id not in seen:
                        seen.add(ad_id)
                        normalized = self._normalize(ad)
                        days = normalized.get('days_running')
                        if days is not None and (days < MIN_DAYS or days > MAX_DAYS):
                            continue
                        cta = (normalized.get('cta_text') or '').lower().strip()
                        if cta in ECOMMERCE_CTAS:
                            continue
                        results.append(normalized)

        results.sort(key=lambda a: a.get('days_running') or 0, reverse=True)
        return results[:total_limit]

    def get_service_ads(self, service):
        keywords = SERVICE_KEYWORDS.get(service)
        if not keywords:
            raise ValueError(f'Unknown service: {service}')
        return self._collect(keywords)
