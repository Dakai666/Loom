import sys
import requests
from bs4 import BeautifulSoup
import urllib3
from xml.etree import ElementTree as ET
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


class _FeedTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # No DTDs, local/external entities or network/file resolution in feeds.
        raise ValueError("DTD declarations are not allowed in feeds")


def _local_name(element):
    return element.tag.rsplit('}', 1)[-1].lower()


def _child(entry, *names):
    return next((child for child in entry if _local_name(child) in names), None)


def _text(element):
    return ''.join(element.itertext()).strip() if element is not None else ""


def parse_rss_content(content, source_name, limit=5):
    """
    Parse RSS/Atom/RDF XML; malformed documents are failures, not empty feeds.
    """
    try:
        root = ET.fromstring(content, parser=ET.XMLParser(target=_FeedTreeBuilder()))
        if _local_name(root) not in ('rss', 'feed', 'rdf'):
            raise ValueError("response is not an RSS/Atom/RDF feed")

        items = []
        entries = [entry for entry in root.iter() if _local_name(entry) in ('item', 'entry')]

        for entry in entries:
            # --- Title ---
            title = _text(_child(entry, 'title'))
            if not title: continue

            # --- Link ---
            link = ""
            for link_tag in entry:
                if _local_name(link_tag) == 'link' and link_tag.get('rel', 'alternate') == 'alternate':
                    link = link_tag.get('href') or _text(link_tag)
                    if link:
                        break

            if not link:
                guid = _text(_child(entry, 'guid'))
                if guid.startswith('http'):
                    link = guid

            # --- Time ---
            time_str = _text(_child(entry, 'pubdate', 'published', 'updated', 'date'))

            # --- Summary / Content ---
            raw_summary = ""
            for name in ('encoded', 'description', 'summary', 'content'):
                element = _child(entry, name)
                if element is not None:
                    # Preserve inline XHTML boundaries before stripping markup.
                    raw_summary = (element.text or '') + ''.join(
                        ET.tostring(child, encoding='unicode') for child in element
                    )
                    break

            soup_desc = BeautifulSoup(raw_summary, 'html.parser')
            plain_summary = soup_desc.get_text(separator=' ', strip=True)
            clean_summary = plain_summary[:300] + ("..." if len(plain_summary) > 300 else "")

            # --- Heat ---
            heat = ""
            comments = _text(_child(entry, 'comments'))
            if comments:
                heat = f"{comments} comments"

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
                stale_days = 14 if source_name == "NEJM Alerts" else 7
                if age_days >= stale_days:
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
    RSS/Atom fetcher with HTTP and media-type validation before XML parsing.
    Handles various feed formats (RSS 2.0, Atom, etc.)
    """
    try:
        # User-Agent is critical
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml,application/atom+xml,application/rdf+xml,application/xml,text/xml;q=0.9"
        }

        response = requests.get(url, headers=headers, timeout=15, verify=False)
        response.raise_for_status()
        media_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if media_type not in ('application/rss+xml', 'application/atom+xml',
                              'application/rdf+xml', 'application/xml', 'text/xml'):
            raise ValueError(f"unexpected Content-Type: {media_type or '(missing)'}")

        return parse_rss_content(response.content, source_name, limit)

    except Exception as e:
        print(f"RSS Fetch failed for {url}: {e}", file=sys.stderr)
        # Previously this returned [] with the reason only on stderr, which
        # vanishes under the 08:00 cron run.  Now the reason is recorded.
        record_source_health(source_name, "failed", f"{type(e).__name__}: {e}", 0)
        return []
