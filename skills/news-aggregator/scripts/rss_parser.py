import sys
import requests
from bs4 import BeautifulSoup
import urllib3
import re
from datetime import datetime

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Source health tracking (added 2026-10-24)
#
# Before this, a failed fetch and a feed that simply had no new items both
# surfaced as an empty list, so the only way to tell them apart was to eyeball
# the generated briefing and guess.  These records make the difference explicit
# and machine-readable, so the briefing's "source health" table can be derived
# from evidence instead of written from impression.
# ---------------------------------------------------------------------------

SOURCE_HEALTH: dict[str, dict] = {}


def record_source_health(source_name, status, detail="", item_count=0):
    """Record the outcome of one source fetch.

    status:
      ok       - fetched successfully, has items
      empty    - fetched successfully, but the feed had zero items
      stale    - fetched successfully, but every item is older than the window
      failed   - the fetch itself raised an error
    """
    SOURCE_HEALTH[source_name] = {
        "source": source_name,
        "status": status,
        "detail": detail,
        "item_count": item_count,
    }
    return SOURCE_HEALTH[source_name]


def clear_source_health():
    """Reset the health records.  Used between independent fetch runs."""
    SOURCE_HEALTH.clear()


def source_health_report():
    """Return the health records collected so far, as a plain list."""
    return list(SOURCE_HEALTH.values())


def health_summary_line():
    """One-line-per-source summary, suitable for stdout/stderr logging."""
    if not SOURCE_HEALTH:
        return "(no source health records)"
    lines = []
    for rec in SOURCE_HEALTH.values():
        detail = f" — {rec['detail']}" if rec["detail"] else ""
        lines.append(
            f"[health] {rec['source']}: {rec['status']} "
            f"({rec['item_count']} items){detail}"
        )
    return "\n".join(lines)


def clean_text(text):
    if not text: return ""
    return text.strip()


def _parse_pub_datetime(time_str):
    """Best-effort parse of a feed timestamp into a datetime.

    Returns None when the timestamp is missing or unparseable — which is
    itself a signal, because a feed with no timestamps at all is one we
    cannot check for staleness.
    """
    if not time_str:
        return None
    from email.utils import parsedate_to_datetime
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(time_str, fmt)
        except (ValueError, TypeError):
            continue
    try:
        return parsedate_to_datetime(time_str)
    except Exception:
        return None


def parse_rss_content(content, source_name, limit=5):
    """
    Parses RSS/Atom content string (XML or HTML) and returns items.
    """
    try:
        # Use html.parser which is built-in and lenient
        soup = BeautifulSoup(content, 'html.parser')

        items = []
        entries = soup.find_all(['item', 'entry'])

        for entry in entries:
            # --- Title ---
            title_tag = entry.find('title')
            if not title_tag: continue
            title = clean_text(title_tag.get_text())

            # --- Link ---
            link = ""
            link_tag = entry.find('link')
            if link_tag:
                if link_tag.has_attr('href'):
                    link = link_tag['href']
                elif link_tag.get_text(strip=True):
                    link = link_tag.get_text(strip=True)
                if not link:
                    link = str(link_tag.next_sibling).strip()

            if not link:
                guid = entry.find('guid')
                if guid and guid.get_text(strip=True).startswith('http'):
                    link = guid.get_text(strip=True)

            # --- Time ---
            pub = entry.find(['pubdate', 'published', 'updated', 'dc:date'])
            time_str = clean_text(pub.get_text()) if pub else ""

            # --- Summary / Content ---
            content_encoded = entry.find('content:encoded')
            description = entry.find('description')
            summary = entry.find('summary')
            content = entry.find('content')

            raw_summary = ""
            if content_encoded: raw_summary = content_encoded.get_text()
            elif description: raw_summary = description.get_text()
            elif summary: raw_summary = summary.get_text()
            elif content: raw_summary = content.get_text()

            soup_desc = BeautifulSoup(raw_summary, 'html.parser')
            clean_summary = soup_desc.get_text(separator=' ', strip=True)[:300] + "..." if len(raw_summary) > 300 else ""

            # --- Heat ---
            heat = ""
            comments = entry.find('slash:comments')
            if comments:
                heat = f"{comments.get_text(strip=True)} comments"

            items.append({
                "source": source_name,
                "title": title,
                "url": link,
                "time": time_str,
                "heat": heat,
                "summary": clean_summary
            })
            if len(items) >= limit: break

        if not items:
            record_source_health(source_name, "empty", "feed parsed but contained zero items", 0)
        else:
            # A feed whose newest item is old is a different failure from a
            # feed with nothing in it — it means the feed is being served but
            # is not publishing.  The briefing should say so rather than
            # silently presenting stale headlines as news.
            dated = [(i, _parse_pub_datetime(i.get("time"))) for i in items]
            dated = [(i, d) for i, d in dated if d is not None]
            if dated:
                newest = max(d for _, d in dated)
                now = datetime.now(newest.tzinfo) if newest.tzinfo else datetime.now()
                age_days = (now - newest).days
                if age_days >= 7:
                    record_source_health(
                        source_name,
                        "stale",
                        f"newest item is {age_days} days old ({newest:%Y-%m-%d})",
                        len(items),
                    )
                else:
                    record_source_health(source_name, "ok", "", len(items))
            else:
                # No parseable timestamps at all.  But before calling this 'ok',
                # check the items are actually usable: a malformed feed parsed
                # by the lenient HTML parser can yield phantom items with no
                # URL and no time.  Those are not news — reporting them as 'ok'
                # would make a dead source look healthy again.
                usable = [i for i in items if i.get("url", "").strip()]
                if not usable:
                    record_source_health(
                        source_name,
                        "failed",
                        f"parsed {len(items)} item(s) but none had a URL "
                        f"(likely malformed feed)",
                        0,
                    )
                else:
                    record_source_health(
                        source_name, "ok", "no parseable timestamps", len(usable)
                    )

        return items
    except Exception as e:
        print(f"Content Parse failed: {e}", file=sys.stderr)
        record_source_health(source_name, "failed", f"parse error: {e}", 0)
        return []


def fetch_rss_feed(url, source_name, limit=5):
    """
    Robust RSS/Atom fetcher using BeautifulSoup.
    Handles various feed formats (RSS 2.0, Atom, etc.)
    """
    try:
        # User-Agent is critical
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }

        response = requests.get(url, headers=headers, timeout=15, verify=False)
        response.encoding = response.apparent_encoding or 'utf-8'

        return parse_rss_content(response.content, source_name, limit)

    except Exception as e:
        print(f"RSS Fetch failed for {url}: {e}", file=sys.stderr)
        # Previously this returned [] with the reason only on stderr, which
        # vanishes under the 08:00 cron run.  Now the reason is recorded.
        record_source_health(source_name, "failed", f"{type(e).__name__}: {e}", 0)
        return []
