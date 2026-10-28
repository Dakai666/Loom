import argparse
import json
import requests
from bs4 import BeautifulSoup
import sys
import time
import re
import concurrent.futures
import os
from datetime import datetime
import subprocess

# Headers for scraping to avoid basic bot detection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

from bs4 import XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

def filter_items(items, keyword=None):
    if not keyword:
        return items
    keywords = [k.strip() for k in keyword.split(',') if k.strip()]
    pattern = '|'.join([r'\b' + re.escape(k) + r'\b' for k in keywords])
    regex = r'(?i)(' + pattern + r')'
    return [item for item in items if re.search(regex, item['title'])]

def fetch_url_content(url):
    """
    Fetches the content of a URL and extracts text from paragraphs.
    Truncates to 3000 characters.
    """
    if not url or not url.startswith('http'):
        return ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
         # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.extract()
        # Get text
        text = soup.get_text(separator=' ', strip=True)
        # Simple cleanup
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        return text[:3000]
    except Exception:
        return ""

def enrich_items_with_content(items, max_workers=10):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {executor.submit(fetch_url_content, item['url']): item for item in items}
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                content = future.result()
                if content:
                    item['content'] = content
            except Exception:
                item['content'] = ""
    return items

# ─────────────────────────────────────────────────────────────────────────────
# 重疊來源：從 opencli_fetch.py 接管
# 2026-04-19 更新：hackernews / producthunt / v2ex 由 OpenCLI adapter 提供，
# 統一維護口徑，消除重複邏輯。
# ─────────────────────────────────────────────────────────────────────────────
from opencli_fetch import fetch_hackernews, fetch_producthunt, fetch_v2ex

# ─────────────────────────────────────────────────────────────────────────────
# 本地 Source Fetchers（僅保留 OpenCLI 無 adapter 的來源）
# ─────────────────────────────────────────────────────────────────────────────

def fetch_weibo(limit=5, keyword=None):
    url = "https://weibo.com/ajax/side/hotSearch"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://weibo.com/"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        items = data.get('data', {}).get('realtime', [])
        all_items = []
        for item in items:
            title = item.get('note', '') or item.get('word', '')
            if not title: continue
            heat = item.get('num', 0)
            full_url = f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}&Refer=top"
            all_items.append({
                "source": "Weibo Hot Search",
                "title": title,
                "url": full_url,
                "heat": f"{heat}",
                "time": "Real-time"
            })
        return filter_items(all_items, keyword)[:limit]
    except Exception:
        return []

def fetch_github(limit=5, keyword=None):
    if keyword:
         topic_url = f"https://github.com/topics/{keyword.split(',')[0].strip()}?o=desc&s=updated"
         try:
             response = requests.get(topic_url, headers=HEADERS, timeout=10)
             if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                items = []
                for article in soup.select('article.border'):
                     h3 = article.select_one('h3 a')
                     if not h3: continue
                     repo_link = h3['href']
                     title = repo_link.strip('/')
                     link = "https://github.com" + repo_link
                     desc = ""
                     desc_div = article.select_one('.color-fg-muted')
                     if desc_div: desc = desc_div.get_text(strip=True)
                     items.append({
                        "source": "GitHub Trending",
                        "title": f"{title} - {desc}",
                        "url": link,
                        "heat": "Topic Match",
                        "time": "Updated recently"
                     })
                if items: return items[:limit]
         except: pass

    try:
        response = requests.get("https://github.com/trending", headers=HEADERS, timeout=10)
    except: return []

    soup = BeautifulSoup(response.text, 'html.parser')
    items = []
    for article in soup.select('article.Box-row'):
        try:
            h2 = article.select_one('h2 a')
            if not h2: continue
            title = h2.get_text(strip=True).replace('\n', '').replace(' ', '')
            link = "https://github.com" + h2['href']
            desc = article.select_one('p')
            desc_text = desc.get_text(strip=True) if desc else ""
            stars_tag = article.select_one('a[href$="/stargazers"]')
            stars = stars_tag.get_text(strip=True) if stars_tag else ""
            items.append({
                "source": "GitHub Trending",
                "title": f"{title} - {desc_text}",
                "url": link,
                "heat": f"{stars} stars",
                "time": "Today"
            })
        except: continue
    return filter_items(items, keyword)[:limit]

def fetch_36kr(limit=5, keyword=None):
    try:
        response = requests.get("https://36kr.com/newsflashes", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = []
        for item in soup.select('.newsflash-item'):
            title = item.select_one('.item-title').get_text(strip=True)
            href = item.select_one('.item-title')['href']
            time_tag = item.select_one('.time')
            time_str = time_tag.get_text(strip=True) if time_tag else ""
            items.append({
                "source": "36Kr",
                "title": title,
                "url": f"https://36kr.com{href}" if not href.startswith('http') else href,
                "time": time_str,
                "heat": ""
            })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_tencent(limit=5, keyword=None):
    try:
        url = "https://i.news.qq.com/web_backend/v2/getTagInfo?tagId=aEWqxLtdgmQ%3D"
        data = requests.get(url, headers={"Referer": "https://news.qq.com/"}, timeout=10).json()
        items = []
        for news in data['data']['tabs'][0]['articleList']:
            items.append({
                "source": "Tencent News",
                "title": news['title'],
                "url": news.get('url') or news.get('link_info', {}).get('url'),
                "time": news.get('pub_time', '') or news.get('publish_time', '')
            })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_wallstreetcn(limit=5, keyword=None):
    try:
        url = "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=30"
        data = requests.get(url, timeout=10).json()
        items = []
        for item in data['data']['items']:
            res = item.get('resource')
            if res and (res.get('title') or res.get('content_short')):
                 ts = res.get('display_time', 0)
                 time_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else ""
                 items.append({
                     "source": "Wall Street CN",
                     "title": res.get('title') or res.get('content_short'),
                     "url": res.get('uri'),
                     "time": time_str
                 })
        return filter_items(items, keyword)[:limit]
    except: return []

# --- RSS-based Fetchers ---

from rss_parser import fetch_rss_feed, source_health_report, health_summary_line, clear_source_health

def fetch_huggingface_papers(limit=5, keyword=None):
    items = []
    try:
        script_path = os.path.join(os.path.dirname(__file__), "fetch_hf_papers_playwright.py")
        cmd = [sys.executable, script_path, "--limit", str(limit)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            for paper in data:
                items.append({
                    "source": "HF Papers",
                    "title": paper['title'],
                    "url": paper['url'],
                    "github": paper.get('github', ''),
                    "heat": paper.get('heat', ''),
                    "time": datetime.now().strftime("%Y-%m-%d"),
                    "summary": paper.get('summary', '')
                })
        else:
             print(f"HF Playwright Failed: {result.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"HF Playwright Exception: {e}", file=sys.stderr)
    return filter_items(items[:limit], keyword)


def fetch_latentspace_ainews(limit=5, keyword=None):
    items = []
    try:
        response = requests.get("https://www.latent.space/feed", headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for entry in soup.find_all('item'):
            title_tag = entry.find('title')
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if not title.startswith('[AINews]'):
                continue
            guid_tag = entry.find('guid')
            link = guid_tag.get_text(strip=True) if guid_tag else ""
            if not link:
                link_tag = entry.find('link')
                if link_tag:
                    link = link_tag.get_text(strip=True) or (link_tag.get('href') or '')
            pub_tag = entry.find('pubdate') or entry.find('published')
            pub_date = pub_tag.get_text(strip=True) if pub_tag else ""
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                pub_date = dt.strftime('%Y-%m-%d')
            except Exception:
                pass
            desc_tag = entry.find('description')
            content = ""
            if desc_tag:
                desc_html = desc_tag.get_text(strip=True)
                desc_soup = BeautifulSoup(desc_html, 'html.parser')
                content = desc_soup.get_text(separator=' ', strip=True)[:2000]
            items.append({
                "source": "Latent Space AINews",
                "title": title,
                "url": link,
                "time": pub_date,
                "heat": "Daily Roundup",
                "content": content
            })
    except Exception as e:
        print(f"Latent Space AINews fetch error: {e}", file=sys.stderr)
    return filter_items(items[:limit], keyword)


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE GROUPS (四軌分類)
# 2026-04-18: Reuters RSS 移除（DNS 持續失敗超過5天）
# 替代方案：以 Al Jazeera World 新聞填補國際軌覆蓋缺口
# ─────────────────────────────────────────────────────────────────────────────

INTL_SOURCES = [
    ("Al Jazeera",     "https://www.aljazeera.com/xml/rss/all.xml"),
    ("BBC World",     "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NHK World",     "https://www3.nhk.or.jp/rss/news/cat0.xml"),
]

MEDICAL_SOURCES = [
    ("NEJM Alerts",      "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss"),
    ("WHO News",        "https://www.who.int/feeds/entity/csr/don/en/rss.xml"),
    ("Medscape Medical","https://www.medscape.com/xml publishers/smyte/rss?channelId=3445"),
]

ASTRONOMY_SOURCES = [
    ("NASA News",      "https://www.nasa.gov/news-release/feed/"),
    ("ESA News",       "https://www.esa.int/rssfeed/Our_Activities/Space_Science"),
    ("Astronomy.com",  "https://astronomy.com/rss/news"),
]

AI_NEWSLETTER_SOURCES = [
    ("Ben's Bites",     "https://www.bensbites.com/feed"),
    ("Interconnects",   "https://www.interconnects.ai/feed"),
    ("One Useful Thing","https://www.oneusefulthing.org/feed"),
    ("ChinAI",          "https://chinai.substack.com/feed"),
    ("Memia",           "https://memia.substack.com/feed"),
    ("AI to ROI",       "https://ai2roi.substack.com/feed"),
    ("KDnuggets",       "https://www.kdnuggets.com/feed"),
]

PODCAST_SOURCES = [
    ("Lex Fridman",  "https://lexfridman.com/feed/podcast"),
    ("80000 Hours",  "https://feeds.transistor.fm/80-000-hours-podcast"),
    ("Latent Space", "https://latent.space/feed"),
]

ESSAY_SOURCES = [
    ("Wait But Why",  "https://waitbutwhy.com/feed"),
    ("James Clear",   "https://jamesclear.com/feed"),
    ("Farnam Street", "https://fs.blog/feed"),
    ("Paul Graham",   "http://www.aaronsw.com/2002/feeds/pgessays.rss"),
    ("Scott Young",   "https://www.scotthyoung.com/blog/feed/"),
    ("Dan Koe",       "https://thedankoe.com/feed/"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 聚合 Fetchers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_international(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, limit): name for name, url in INTL_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_medical(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, limit): name for name, url in MEDICAL_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_astronomy(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, limit): name for name, url in ASTRONOMY_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_ai_newsletters(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in AI_NEWSLETTER_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_podcasts(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in PODCAST_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_essays(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in ESSAY_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_rss_with_playwright(url, source_name, limit=5):
    try:
        if "Ben's Bites" in source_name:
             script_path = os.path.join(os.path.dirname(__file__), "fetch_bensbites.py")
             cmd = [sys.executable, script_path]
             result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
             if result.returncode == 0:
                 try:
                    data = json.loads(result.stdout)
                    if not data: raise ValueError("Empty JSON")
                    return data
                 except Exception:
                    return [{"source": "Ben's Bites", "title": "Ben's Bites (Visit Site)", "url": "https://bensbites.beehiiv.com/", "time": "Today", "summary": "Auto-fetch failed. Please verify on site."}]
             else:
                 return [{"source": "Ben's Bites", "title": "Ben's Bites (Check Site)", "url": "https://bensbites.beehiiv.com/", "time": "Today", "summary": "Fetch process failed."}]
        return []
    except Exception as e:
        print(f"Playwright exception for {source_name}: {e}", file=sys.stderr)
        return []

def create_single_rss_fetcher(url, name):
    def fetcher(limit=5, keyword=None):
        return filter_items(fetch_rss_feed(url, name, limit), keyword)[:limit]
    return fetcher

def save_report(data, source_name, out_dir):
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    safe_name = "".join([c if c.isalnum() else "_" for c in source_name]).lower()
    timestamp = datetime.now().strftime("%H%M")
    json_path = os.path.join(out_dir, f"{safe_name}_{timestamp}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return json_path


# ─────────────────────────────────────────────────────────────────────────────
# sources_map — 完整覆蓋（OpenCLI 接管重疊來源後，key 不變，向後相容）
# ─────────────────────────────────────────────────────────────────────────────

sources_map = {
    # ── OpenCLI 接管（from opencli_fetch.py）─────────────
    'hackernews':  fetch_hackernews,    # opencli_fetch
    'producthunt': fetch_producthunt,   # opencli_fetch
    'v2ex':        fetch_v2ex,          # opencli_fetch
    # ── 本地爬蟲 ────────────────────────────────────────
    'weibo':        fetch_weibo,
    'github':       fetch_github,
    '36kr':         fetch_36kr,
    'tencent':      fetch_tencent,
    'wallstreetcn': fetch_wallstreetcn,
    'huggingface': fetch_huggingface_papers,
    # ── 聚合軌 ──────────────────────────────────────────
    'ai_newsletters':    fetch_ai_newsletters,
    'podcasts':          fetch_podcasts,
    'essays':            fetch_essays,
    'latentspace_ainews': fetch_latentspace_ainews,
    'international':     fetch_international,
    'medical':           fetch_medical,
    'astronomy':         fetch_astronomy,
}

# Dynamic Registration of Sub-sources
for name, url in AI_NEWSLETTER_SOURCES:
    key = name.lower().replace(' ', '').replace("'", "")
    if "Ben's Bites" in name:
         sources_map[key] = lambda limit=10, k=None, u=url, n=name: filter_items(fetch_rss_with_playwright(u, n, limit), k)[:limit]
    else:
         sources_map[key] = create_single_rss_fetcher(url, name)

for name, url in PODCAST_SOURCES:
    key = name.lower().replace(' ', '')
    sources_map[key] = create_single_rss_fetcher(url, name)

for name, url in ESSAY_SOURCES:
    key = name.lower().replace(' ', '')
    sources_map[key] = create_single_rss_fetcher(url, name)

for name, url in INTL_SOURCES:
    key = name.lower().replace(' ', '_').replace("'", "")
    sources_map[key] = create_single_rss_fetcher(url, name)

for name, url in MEDICAL_SOURCES:
    key = name.lower().replace(' ', '_')
    sources_map[key] = create_single_rss_fetcher(url, name)

for name, url in ASTRONOMY_SOURCES:
    key = name.lower().replace(' ', '_')
    sources_map[key] = create_single_rss_fetcher(url, name)

# ─────────────────────────────────────────────────────────────────────────────
# 四軌一次 Fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_four_tracks(limit_per_track=8, keyword=None):
    tracks = {}
    track_configs = [
        ("tech",          ["hackernews", "github"]),
        ("international", ["international"]),
        ("medical",       ["medical"]),
        ("astronomy",     ["astronomy"]),
    ]
    for track_name, source_keys in track_configs:
        track_items = []
        for key in source_keys:
            if key in sources_map:
                try:
                    track_items.extend(sources_map[key](limit_per_track, keyword))
                except Exception as e:
                    print(f"[{track_name}] {key} failed: {e}", file=sys.stderr)
        tracks[track_name] = track_items
    return tracks


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='all', help='Source(s) to fetch from (comma-separated)')
    parser.add_argument('--limit', type=int, default=10, help='Limit per source. Default 10')
    parser.add_argument('--keyword', help='Comma-sep keyword filter')
    parser.add_argument('--deep', action='store_true', help='Download article content for detailed summarization')
    parser.add_argument('--save', action='store_true', help='Save output to reports directory (JSON + MD)')
    parser.add_argument('--no-save', action='store_true', dest='no_save', help='Skip saving JSON files to disk')
    parser.add_argument('--outdir', help='Custom output directory for saved reports')
    parser.add_argument('--list-sources', action='store_true', help='List all available source keys')
    parser.add_argument('--all-tracks', action='store_true', help='Fetch all four tracks and output as JSON dict')

    args = parser.parse_args()

    if args.list_sources:
        print(f"{'Source Key':<30} | {'Source Name'}")
        print("-" * 50)
        for key in sorted(sources_map.keys()):
            print(f"{key:<30}")
        return

    if args.all_tracks:
        clear_source_health()
        tracks = fetch_all_four_tracks(limit_per_track=args.limit, keyword=args.keyword)
        # Source health rides along with the data (2026-10-24).
        #
        # Before this, the briefing's "source health" table was written by me
        # from impression — the script returned [] for both "the fetch failed"
        # and "the feed has no new items", so there was nothing to cite.
        # Now the distinction is machine-produced and travels with the JSON.
        tracks["_source_health"] = source_health_report()
        print(json.dumps(tracks, indent=2, ensure_ascii=False))
        print("\n" + health_summary_line(), file=sys.stderr)
        return

    to_run = []
    if args.source == 'all':
        to_run = list(sources_map.values())
    else:
        requested_sources = [s.strip() for s in args.source.split(',')]
        for s in requested_sources:
            if s in sources_map: to_run.append(sources_map[s])

    results = []

    def run_fetchers(fetchers, limit, kw):
        res = []
        for func in fetchers:
            try:
                res.extend(func(limit, kw))
            except: pass
        return res

    results = run_fetchers(to_run, args.limit, args.keyword)

    # Smart Fill Logic
    MIN_ITEMS = 5
    if args.keyword and len(results) < MIN_ITEMS:
        sys.stderr.write(f"Smart Fill triggered: Found {len(results)} items, filling gaps...\n")
        fill_results = run_fetchers(to_run, limit=MIN_ITEMS, kw=None)
        existing_urls = {item.get('url') for item in results}
        existing_titles = {item.get('title') for item in results}
        for item in fill_results:
            if len(results) >= MIN_ITEMS:
                break
            u = item.get('url')
            t = item.get('title')
            if u not in existing_urls and t not in existing_titles:
                item['smart_fill'] = True
                if 'time' in item:
                    item['time'] = f"⚠️ {item['time']}"
                results.append(item)
                existing_urls.add(u)
                existing_titles.add(t)

    if args.deep and results:
        sys.stderr.write(f"Deep fetching content for {len(results)} items...\n")
        results = enrich_items_with_content(results)

    print(json.dumps(results, indent=2, ensure_ascii=False))
    health = source_health_report()
    if health:
        print("\n" + health_summary_line(), file=sys.stderr)

    if not getattr(args, 'no_save', False) and (args.save or args.source != 'all'):
        if args.outdir:
            out_dir = args.outdir
        else:
            today = datetime.now().strftime('%Y-%m-%d')
            out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'reports', today)
        save_report(results, args.source, out_dir)
        health = source_health_report()
        if health:
            health_path = os.path.join(out_dir, f"{args.source}_source_health.json")
            with open(health_path, "w", encoding="utf-8") as f:
                json.dump(health, f, indent=2, ensure_ascii=False)
            print(f"[health] saved -> {health_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
