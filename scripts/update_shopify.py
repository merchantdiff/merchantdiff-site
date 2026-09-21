from urllib.request import Request, urlopen
from urllib.parse import urlparse, urlencode
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape
import hashlib
import json
import os
import re
import time


FEED_URL = "https://shopify.dev/changelog/feed.xml"

SITE_URL = "https://merchantdiff.github.io/merchantdiff-site/"
BOOSTY_URL = "https://boosty.to/merchantdiff"
X_URL = "https://x.com/MerchantDiff"

ANALYTICS_TOKEN = "44ece3bc3eee498c9bed2bbfd20a997c"

CHANGES_DIR = Path("changes")
INDEX_FILE = Path("index.html")
UPDATES_FILE = Path("updates.html")
SITEMAP_FILE = Path("sitemap.xml")
ROBOTS_FILE = Path("robots.txt")

MAX_FEED_ITEMS = 200
MAX_UPDATES_ON_INDEX = 30
READY_TO_POST_LIMIT = 5
RELATED_LIMIT = 3
FEED_RETRIES = 3
FEED_TIMEOUT = 30

# Increment when the meaning or layout of generated change pages changes.
# This lets MerchantDiff keep a stable, honest dateModified/lastmod until a
# page actually changes again.
CHANGE_PAGE_TEMPLATE_VERSION = "2026-09-21-v4"

GENERIC_CATEGORIES = {
    "api",
    "new",
    "update",
    "action required",
    "breaking api change",
    "breaking change",
    "deprecation announcement",
    "deprecation",
}

TECHNICAL_CATEGORY_SIGNALS = [
    "api",
    "graphql",
    "rest",
    "webhook",
    "events",
    "checkout",
    "customer account",
    "pos",
    "function",
    "extension",
    "metafield",
    "theme",
    "storefront",
    "hydrogen",
    "oxygen",
    "app bridge",
    "cli",
]

SURFACE_TERMS = {
    "graphql",
    "webhook",
    "webhooks",
    "checkout",
    "pos",
    "discount",
    "discounts",
    "metafield",
    "metafields",
    "order",
    "orders",
    "fulfillment",
    "fulfillments",
    "function",
    "functions",
    "storefront",
    "customer",
    "theme",
    "themes",
    "extension",
    "extensions",
    "inventory",
    "product",
    "products",
    "variant",
    "variants",
    "payment",
    "payments",
    "shipping",
    "markets",
    "token",
    "tokens",
}


# ---------------------------------------------------------------------------
# TEXT / FEED HELPERS
# ---------------------------------------------------------------------------


def safe_slug(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def page_slug(title, link):
    parsed = urlparse(link)
    last_part = parsed.path.rstrip("/").split("/")[-1]

    slug = safe_slug(last_part)

    if slug:
        return slug

    slug = safe_slug(title)

    if slug:
        return slug

    digest = hashlib.sha1(
        link.encode("utf-8")
    ).hexdigest()[:10]

    return f"shopify-change-{digest}"


class ShopifyHTMLTextParser(HTMLParser):
    """Convert Shopify changelog HTML fragments into readable plain text.

    Block boundaries are preserved as newlines so source headings such as
    "What changed" and "Who's affected" can be identified reliably.
    """

    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dl",
        "dt",
        "dd",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def _break(self):
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self._break()

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def get_text(self):
        raw = "".join(self.parts)
        lines = []

        for line in raw.splitlines():
            line = re.sub(r"\s+", " ", line).strip()

            if line:
                lines.append(line)
            elif lines and lines[-1] != "":
                lines.append("")

        while lines and lines[-1] == "":
            lines.pop()

        return "\n".join(lines)


def clean_description(html_text):
    if not html_text:
        return ""

    parser = ShopifyHTMLTextParser()

    try:
        parser.feed(html_text)
        parser.close()
        text = parser.get_text()
    except Exception:
        # Conservative fallback: never let malformed source HTML stop updates.
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = re.sub(r"\s+", " ", text).strip()

    return text.strip()


def compact_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def smart_truncate(text, limit, max_sentences=None):
    text = compact_text(text)

    if not text:
        return ""

    if max_sentences:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        selected = []
        length = 0

        for sentence in sentences:
            sentence = sentence.strip()

            if not sentence:
                continue

            projected = length + len(sentence) + (1 if selected else 0)

            if selected and projected > limit:
                break

            selected.append(sentence)
            length = projected

            if len(selected) >= max_sentences:
                break

        if selected:
            candidate = " ".join(selected)

            if len(candidate) <= limit:
                return candidate

    if len(text) <= limit:
        return text

    cutoff = text[: limit + 1]
    minimum_sentence_end = int(limit * 0.55)
    sentence_ends = [
        match.end()
        for match in re.finditer(r"[.!?](?:\s|$)", cutoff)
        if match.end() >= minimum_sentence_end
    ]

    if sentence_ends:
        return cutoff[: sentence_ends[-1]].strip()

    shortened = cutoff[:limit].rsplit(" ", 1)[0].strip()

    if not shortened:
        shortened = cutoff[:limit].strip()

    return shortened.rstrip(" ,;:") + "…"


def normalize_heading(text):
    text = compact_text(text).lower()
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", "", text)
    return text.strip()


def parse_description_sections(description):
    """Extract Shopify-authored sections when the feed provides headings."""

    heading_patterns = [
        (
            re.compile(
                r"^(?:what changed|what(?:'s| is) changed|"
                r"what(?:'s| is) changing|what is new)\s*:?\s*",
                re.IGNORECASE,
            ),
            "what",
        ),
        (
            re.compile(
                r"^(?:who(?:'s| is) affected|affected apps|"
                r"affected developers)\s*:?\s*",
                re.IGNORECASE,
            ),
            "audience",
        ),
        (
            re.compile(
                r"^(?:what you need to do|what you should do|what to do|"
                r"required action|action required|developer action|"
                r"next steps|migration guidance)\s*:?\s*",
                re.IGNORECASE,
            ),
            "action",
        ),
    ]

    sections = {
        "intro": [],
        "what": [],
        "audience": [],
        "action": [],
    }

    current = "intro"

    for raw_line in (description or "").splitlines():
        line = compact_text(raw_line)

        if not line:
            continue

        normalized_line = line.replace("’", "'")
        matched = False

        for pattern, section_name in heading_patterns:
            match = pattern.match(normalized_line)

            if not match:
                continue

            current = section_name
            remainder = line[match.end():].strip()

            if remainder:
                sections[current].append(remainder)

            matched = True
            break

        if matched:
            continue

        sections[current].append(line)

    return {
        key: compact_text(" ".join(value))
        for key, value in sections.items()
    }


def extract_api_versions(title, categories, description):
    pattern = r"\b20\d{2}-(?:01|04|07|10)\b"

    primary_text = " ".join(
        [
            title or "",
            " ".join(categories or []),
        ]
    )

    primary_versions = list(
        dict.fromkeys(
            re.findall(
                pattern,
                primary_text,
            )
        )
    )

    if primary_versions:
        return primary_versions

    description_versions = list(
        dict.fromkeys(
            re.findall(
                pattern,
                description or "",
            )
        )
    )

    # If the body mentions several releases, don't guess which one is the
    # actual target version. A single version is safe enough to surface.
    if len(description_versions) == 1:
        return description_versions

    return []


def parse_date(pub_date):
    try:
        parsed = parsedate_to_datetime(pub_date)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return {
            "display": parsed.strftime("%B %d, %Y"),
            "iso_date": parsed.strftime("%Y-%m-%d"),
            "iso_datetime": parsed.isoformat(),
        }

    except Exception:
        return {
            "display": pub_date,
            "iso_date": "",
            "iso_datetime": "",
        }


def build_audience_text(title, categories, description, source_audience=""):
    if source_audience:
        return smart_truncate(
            source_audience,
            440,
            max_sentences=3,
        )

    combined = " ".join(
        [
            title or "",
            " ".join(categories or []),
            description or "",
        ]
    ).lower()

    audience_rules = [
        (
            ["webhook", "webhooks", "event payload", "events payload"],
            "Apps that subscribe to or process Shopify webhooks or event "
            "payloads should review this change.",
        ),
        (
            ["pos ui extension", "pos ui extensions", " point of sale", "pos "],
            "Developers maintaining Shopify POS apps or POS UI extensions "
            "should review this change.",
        ),
        (
            ["customer account", "customer accounts"],
            "Developers building customer account experiences or extensions "
            "should review this change.",
        ),
        (
            ["checkout ui", "checkout extension", "checkout"],
            "Developers building or maintaining Shopify checkout "
            "customizations should review this change.",
        ),
        (
            ["shopify function", "shopify functions", "function api"],
            "Developers building Shopify Functions should review this change.",
        ),
        (
            ["metafield", "metafields"],
            "Apps that read, write, translate, or depend on Shopify metafields "
            "should review this change.",
        ),
        (
            ["discount", "discounts"],
            "Apps that create, manage, or depend on Shopify discount "
            "functionality should review this change.",
        ),
        (
            ["storefront api", "storefront"],
            "Apps and storefronts using the affected Storefront API surface "
            "should review this change.",
        ),
        (
            ["admin graphql api", "graphql"],
            "Apps using the affected Shopify GraphQL API surface should "
            "review this change.",
        ),
        (
            ["rest api"],
            "Apps still using the affected Shopify REST API surface should "
            "review this change.",
        ),
        (
            ["access token", "access tokens", "oauth", "authentication"],
            "Apps that create, store, refresh, or validate Shopify access "
            "credentials should review this change.",
        ),
        (
            ["theme", "themes", "liquid"],
            "Theme and storefront developers using the affected Shopify "
            "surface should review this change.",
        ),
        (
            ["fulfillment", "fulfillments"],
            "Apps that manage fulfillment workflows should review this change.",
        ),
        (
            ["order", "orders"],
            "Apps that read or manage Shopify order data should review this "
            "change.",
        ),
    ]

    for signals, message in audience_rules:
        if any(signal in combined for signal in signals):
            return message

    return (
        "Shopify app developers using the API or platform feature named in "
        "this update should review the official change."
    )


def build_action_text(title, categories, source_action=""):
    if source_action:
        return smart_truncate(
            source_action,
            520,
            max_sentences=4,
        )

    classification_text = " ".join(
        [
            title or "",
            " ".join(categories or []),
        ]
    ).lower()

    removal_signals = [
        "removed",
        "removal",
        "no longer supported",
        "no longer available",
        "sunset",
    ]

    deprecation_signals = [
        "deprecated",
        "deprecation",
    ]

    additive_signals = [
        "new",
        "adds",
        "added",
        "introduces",
        "available",
        "now supports",
        "support for",
    ]

    if any(signal in classification_text for signal in removal_signals):
        return (
            "Check your codebase for use of the affected surface. If it is in "
            "use, review Shopify's official migration guidance and replace or "
            "remove the dependency before adopting the affected API version."
        )

    if any(signal in classification_text for signal in deprecation_signals):
        return (
            "Check whether your app uses the deprecated surface. If it does, "
            "plan the migration using Shopify's official guidance before the "
            "applicable cutoff."
        )

    if (
        "action required" in classification_text
        or "breaking" in classification_text
    ):
        return (
            "Review the official Shopify entry and test the affected "
            "integration. Apply any required code or configuration changes "
            "before deploying."
        )

    if any(signal in classification_text for signal in additive_signals):
        return (
            "Review whether the new capability is relevant to your app. If you "
            "plan to use it, test the affected API or workflow before adopting "
            "it in production."
        )

    return (
        "Review the official Shopify entry, confirm whether the change touches "
        "your app, and test the affected workflow before your next relevant "
        "deployment."
    )


def is_important(title, categories):
    combined = " ".join(
        [
            title or "",
            " ".join(categories or []),
        ]
    ).lower()

    important_signals = [
        "action required",
        "breaking api change",
        "breaking change",
        "breaking changes",
        "deprecation announcement",
        "deprecated",
        "deprecation",
        "removed",
        "removal",
        "sunset",
    ]

    return any(signal in combined for signal in important_signals)


def is_seo_worthy(title, categories, description):
    category_text = " ".join(categories or []).lower()
    title_text = (title or "").lower()
    description_text = compact_text(description)

    strong_signals = [
        "action required",
        "breaking api change",
        "breaking change",
        "deprecation announcement",
        "deprecation",
        "deprecated",
        "removed",
        "removal",
        "sunset",
        "deadline",
        "no longer supported",
        "no longer available",
        "migration required",
    ]

    if any(
        signal in category_text or signal in title_text
        for signal in strong_signals
    ):
        return True

    technical_category_match = any(
        signal in category_text
        for signal in TECHNICAL_CATEGORY_SIGNALS
    )

    if technical_category_match and len(description_text) >= 160:
        return True

    technical_title_signals = [
        "graphql",
        "rest api",
        "webhook",
        "checkout",
        "shopify functions",
        "access token",
        "metafield",
        "extension",
        "storefront api",
        "customer account",
    ]

    change_signals = [
        "change",
        "update",
        "new",
        "removed",
        "deprecated",
        "require",
        "available",
        "support",
    ]

    return (
        any(signal in title_text for signal in technical_title_signals)
        and any(signal in title_text for signal in change_signals)
    )


def is_x_worthy(title, categories, description, important, seo_worthy):
    if important:
        return True

    if not seo_worthy:
        return False

    combined = " ".join(
        [
            title or "",
            " ".join(categories or []),
        ]
    ).lower()

    notable_signals = [
        "new",
        "available",
        "now supports",
        "support for",
        "launch",
        "stable",
        "developer preview",
        "general availability",
    ]

    return (
        any(signal in combined for signal in notable_signals)
        and len(compact_text(description)) >= 160
    )


def related_score(current, candidate):
    if current["slug"] == candidate["slug"]:
        return -1

    score = 0

    current_categories = {
        value.lower()
        for value in current["categories"]
        if value and value.lower() not in GENERIC_CATEGORIES
    }

    candidate_categories = {
        value.lower()
        for value in candidate["categories"]
        if value and value.lower() not in GENERIC_CATEGORIES
    }

    score += 7 * len(current_categories & candidate_categories)

    current_versions = set(current.get("api_versions", []))
    candidate_versions = set(candidate.get("api_versions", []))
    score += 4 * len(current_versions & candidate_versions)

    def words(value):
        return set(re.findall(r"[a-z0-9]+", (value or "").lower()))

    current_surface = words(
        " ".join(
            [
                current["title"],
                " ".join(current["categories"]),
            ]
        )
    ) & SURFACE_TERMS

    candidate_surface = words(
        " ".join(
            [
                candidate["title"],
                " ".join(candidate["categories"]),
            ]
        )
    ) & SURFACE_TERMS

    score += 3 * len(current_surface & candidate_surface)

    stop_words = {
        "shopify",
        "api",
        "version",
        "update",
        "updates",
        "change",
        "changes",
        "developer",
        "developers",
        "the",
        "and",
        "for",
        "from",
        "with",
        "this",
        "that",
        "has",
        "have",
        "are",
        "was",
        "were",
        "into",
        "removed",
        "deprecated",
        "available",
    }

    def title_words(value):
        return {
            word
            for word in re.findall(r"[a-z0-9]+", (value or "").lower())
            if len(word) >= 4 and word not in stop_words
        }

    shared_words = title_words(current["title"]) & title_words(
        candidate["title"]
    )
    score += 2 * len(shared_words)

    return score


def category_label(update):
    if update.get("important"):
        return "Action / breaking change"

    for category in update.get("categories", []):
        normalized = category.lower()

        if normalized in GENERIC_CATEGORIES:
            continue

        if re.fullmatch(r"20\d{2}-(?:01|04|07|10)", category):
            continue

        return category

    return "Shopify developer update"


# ---------------------------------------------------------------------------
# STABLE MODIFICATION DATES / JSON-LD
# ---------------------------------------------------------------------------


def semantic_hash(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def read_existing_page_state(path):
    if not path.exists():
        return "", ""

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return "", ""

    hash_match = re.search(
        r"<!--\s*merchantdiff-content-hash:\s*([a-f0-9]{64})\s*-->",
        text,
    )
    modified_match = re.search(
        r"<!--\s*merchantdiff-lastmod:\s*([^\s]+)\s*-->",
        text,
    )

    return (
        hash_match.group(1) if hash_match else "",
        modified_match.group(1) if modified_match else "",
    )


def now_utc_iso():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def page_modification_time(path, new_hash, published_iso=""):
    old_hash, old_modified = read_existing_page_state(path)

    if old_hash == new_hash and old_modified:
        return old_modified

    current = now_utc_iso()

    if not published_iso:
        return current

    try:
        published = datetime.fromisoformat(
            published_iso.replace("Z", "+00:00")
        )
        now = datetime.fromisoformat(current.replace("Z", "+00:00"))

        if published > now:
            return published.isoformat().replace("+00:00", "Z")
    except Exception:
        pass

    return current


def json_ld_script(data):
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Prevent source-controlled strings from prematurely closing the script.
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


# ---------------------------------------------------------------------------
# ANALYTICS / X
# ---------------------------------------------------------------------------


def analytics_snippet():
    return f"""
<!-- Cloudflare Web Analytics -->
<script
    type="module"
    src="https://static.cloudflareinsights.com/beacon.min.js"
    data-cf-beacon='{{"token":"{ANALYTICS_TOKEN}"}}'>
</script>
<!-- End Cloudflare Web Analytics -->
"""


def build_x_post(title, local_url, important):
    title = (title or "").strip()

    if len(title) > 155:
        title = title[:152].rstrip() + "..."

    intro = (
        "Important Shopify developer change:"
        if important
        else "Shopify developer update:"
    )

    return (
        f"{intro}\n\n"
        f"{title}\n\n"
        f"{local_url}\n\n"
        "#ShopifyDev"
    )


def build_x_intent(post_text):
    query = urlencode({"text": post_text})
    return f"https://x.com/intent/tweet?{query}"


def x_queue_script():
    return """
<script>
const MERCHANTDIFF_POSTED_KEY = "merchantdiff_posted_x_v2";

function getPostedItems() {
    try {
        const stored = localStorage.getItem(MERCHANTDIFF_POSTED_KEY);
        if (!stored) return new Set();
        const values = JSON.parse(stored);
        if (!Array.isArray(values)) return new Set();
        return new Set(values);
    } catch (error) {
        console.warn("Could not read MerchantDiff X queue:", error);
        return new Set();
    }
}

function savePostedItems(posted) {
    try {
        localStorage.setItem(
            MERCHANTDIFF_POSTED_KEY,
            JSON.stringify(Array.from(posted))
        );
    } catch (error) {
        console.warn("Could not save MerchantDiff X queue:", error);
    }
}

function markAsPosted(key) {
    const posted = getPostedItems();
    posted.add(key);
    savePostedItems(posted);
    refreshReadyQueue();
}

function refreshReadyQueue() {
    const posted = getPostedItems();
    const items = document.querySelectorAll(
        ".ready-item[data-post-key]"
    );
    let visibleCount = 0;

    items.forEach((item) => {
        const key = item.dataset.postKey;
        if (posted.has(key)) {
            item.style.display = "none";
        } else {
            item.style.display = "";
            visibleCount += 1;
        }
    });

    const emptyState = document.getElementById("ready-empty-state");
    if (emptyState) {
        emptyState.style.display = visibleCount === 0 ? "block" : "none";
    }

    const counter = document.getElementById("ready-counter");
    if (counter) {
        if (visibleCount === 0) {
            counter.textContent = "Queue cleared";
        } else if (visibleCount === 1) {
            counter.textContent = "1 post waiting";
        } else {
            counter.textContent = `${visibleCount} posts waiting`;
        }
    }
}

function resetPostedMarks() {
    const confirmed = window.confirm(
        "Show all hidden X post candidates again?"
    );
    if (!confirmed) return;
    localStorage.removeItem(MERCHANTDIFF_POSTED_KEY);
    refreshReadyQueue();
}

document.addEventListener("DOMContentLoaded", refreshReadyQueue);
</script>
"""


# ---------------------------------------------------------------------------
# DOWNLOAD / PARSE SHOPIFY FEED
# ---------------------------------------------------------------------------


def download_shopify_feed():
    local_feed = os.environ.get("MERCHANTDIFF_FEED_FILE", "").strip()

    if local_feed:
        return Path(local_feed).read_bytes()

    request = Request(
        FEED_URL,
        headers={
            "User-Agent": (
                "MerchantDiff/1.0 "
                "(+https://merchantdiff.github.io/merchantdiff-site/)"
            )
        },
    )

    last_error = None

    for attempt in range(1, FEED_RETRIES + 1):
        try:
            with urlopen(request, timeout=FEED_TIMEOUT) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error

            if attempt < FEED_RETRIES:
                time.sleep(attempt * 3)

    raise RuntimeError(
        f"Could not download Shopify changelog after {FEED_RETRIES} attempts: "
        f"{last_error}"
    )


def parse_updates(xml_data):
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as error:
        raise RuntimeError(f"Shopify changelog XML is invalid: {error}") from error

    items = root.findall(".//item")[:MAX_FEED_ITEMS]
    updates = []

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (
            item.findtext("link")
            or item.findtext("guid")
            or ""
        ).strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        raw_description = (
            item.findtext("description")
            or item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded"
            )
            or ""
        ).strip()

        if not title or not link:
            continue

        description = clean_description(raw_description)
        sections = parse_description_sections(description)

        categories = list(
            dict.fromkeys(
                (category.text or "").strip()
                for category in item.findall("category")
                if category.text and (category.text or "").strip()
            )
        )

        date = parse_date(pub_date)
        slug = page_slug(title, link)
        local_url = f"{SITE_URL}changes/{slug}.html"

        important = is_important(title, categories)
        seo_worthy = is_seo_worthy(title, categories, description)
        x_worthy = is_x_worthy(
            title,
            categories,
            description,
            important,
            seo_worthy,
        )

        x_post = build_x_post(title, local_url, important)
        x_intent = build_x_intent(x_post)

        updates.append(
            {
                "title": title,
                "source_url": link,
                "date_display": date["display"],
                "date_iso": date["iso_date"],
                "date_iso_datetime": date["iso_datetime"],
                "description": description,
                "sections": sections,
                "api_versions": extract_api_versions(
                    title,
                    categories,
                    description,
                ),
                "categories": categories,
                "important": important,
                "seo_worthy": seo_worthy,
                "x_worthy": x_worthy,
                "slug": slug,
                "local_url": local_url,
                "x_post": x_post,
                "x_intent": x_intent,
            }
        )

    def sort_key(update):
        value = update.get("date_iso_datetime") or ""

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    updates.sort(key=sort_key, reverse=True)
    return updates


# ---------------------------------------------------------------------------
# COMMON CSS
# ---------------------------------------------------------------------------

COMMON_CSS = """
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    background: #f7f8fa;
    color: #161616;
}

a {
    color: #1457d9;
}

button {
    font: inherit;
}

.wrap {
    max-width: 920px;
    margin: auto;
    padding: 30px 20px 70px;
}

.topnav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 42px;
}

.brand {
    font-size: 21px;
    font-weight: 800;
    color: #111;
    text-decoration: none;
}

.navlinks {
    display: flex;
    gap: 16px;
    font-size: 14px;
}

.navlinks a {
    color: #444;
    text-decoration: none;
}

.hero {
    margin-bottom: 34px;
}

h1 {
    font-size: clamp(34px, 6vw, 50px);
    line-height: 1.08;
    margin: 0 0 18px;
    letter-spacing: -0.025em;
}

.intro {
    max-width: 760px;
    font-size: 18px;
    line-height: 1.65;
    color: #555;
}

.ready-panel {
    margin: 0 0 38px;
    padding: 26px;
    border-radius: 18px;
    background: #171717;
    color: white;
}

.ready-panel h2 {
    margin: 0 0 8px;
    font-size: 27px;
}

.ready-intro {
    margin: 0 0 12px;
    max-width: 700px;
    color: #cfcfcf;
    line-height: 1.55;
}

.queue-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 0 0 20px;
}

.queue-counter {
    color: #9b9b9b;
    font-size: 13px;
}

.reset-button {
    padding: 0;
    border: 0;
    background: transparent;
    color: #9b9b9b;
    text-decoration: underline;
    cursor: pointer;
    font-size: 12px;
}

.reset-button:hover {
    color: white;
}

.ready-list {
    display: grid;
    gap: 12px;
}

.ready-item {
    padding: 17px;
    border-radius: 12px;
    background: #262626;
}

.ready-item-title {
    margin: 5px 0 12px;
    font-size: 17px;
    line-height: 1.35;
    font-weight: 700;
}

.ready-meta {
    font-size: 12px;
    color: #aaa;
}

.ready-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
}

.ready-button {
    display: inline-block;
    padding: 9px 14px;
    border-radius: 9px;
    background: white;
    color: #111;
    text-decoration: none;
    font-weight: 800;
    font-size: 14px;
}

.ready-button:hover {
    opacity: 0.9;
}

.ready-page-link {
    color: #ccc;
    font-size: 13px;
}

.ready-empty {
    display: none;
    padding: 22px;
    border-radius: 12px;
    background: #262626;
    color: #cfcfcf;
    text-align: center;
}

.ready-empty strong {
    display: block;
    margin-bottom: 6px;
    color: white;
}

.card {
    background: white;
    border: 1px solid #e3e6ea;
    border-radius: 16px;
    padding: 23px;
    margin: 16px 0;
}

.card h2 {
    margin: 11px 0;
    font-size: 22px;
    line-height: 1.3;
}

.card h2 a {
    color: #161616;
    text-decoration: none;
}

.card h2 a:hover {
    text-decoration: underline;
}

.card-summary {
    margin: 10px 0 0;
    color: #555;
    line-height: 1.6;
}

.meta {
    font-size: 14px;
    color: #666;
}

.tags {
    margin-top: 10px;
}

.tag {
    display: inline-block;
    margin: 3px 6px 3px 0;
    padding: 5px 9px;
    background: #eef1f5;
    border-radius: 20px;
    font-size: 12px;
}

.urgent {
    display: inline-block;
    margin-left: 8px;
    font-weight: 700;
    color: #8b2d16;
}

.actions {
    margin-top: 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
}

.action-button {
    display: inline-block;
    padding: 9px 13px;
    border-radius: 9px;
    background: #111;
    color: white;
    text-decoration: none;
    font-size: 14px;
    font-weight: 700;
}

.action-button:hover {
    opacity: 0.85;
}

.secondary-link {
    font-size: 14px;
}

.detail {
    background: white;
    border: 1px solid #e3e6ea;
    border-radius: 18px;
    padding: 28px;
    margin: 24px 0;
}

.detail p {
    font-size: 17px;
    line-height: 1.7;
    color: #444;
}

.insight-grid {
    display: grid;
    gap: 16px;
    margin: 24px 0;
}

.insight {
    padding: 18px;
    background: #f7f8fa;
    border: 1px solid #e3e6ea;
    border-radius: 12px;
}

.insight h2 {
    margin: 0 0 8px;
    font-size: 20px;
}

.insight p {
    margin: 0;
}

.version-note {
    margin-top: 18px;
    padding: 14px 16px;
    border-left: 4px solid #111;
    background: #f2f4f7;
}

.related {
    margin-top: 32px;
}

.related h2 {
    margin-bottom: 12px;
}

.related-list {
    margin: 0;
    padding-left: 20px;
}

.related-list li {
    margin: 8px 0;
    line-height: 1.5;
}

.source-box {
    margin-top: 24px;
    padding: 18px;
    background: #f2f4f7;
    border-radius: 12px;
}

.cta {
    margin-top: 40px;
    padding: 30px;
    background: #171717;
    color: white;
    border-radius: 18px;
}

.cta h2 {
    margin-top: 0;
}

.cta p {
    line-height: 1.6;
    color: #ddd;
}

.cta a {
    display: inline-block;
    margin-top: 7px;
    color: white;
    font-weight: 700;
}

footer {
    margin-top: 44px;
    padding-top: 20px;
    border-top: 1px solid #ddd;
    font-size: 13px;
    line-height: 1.6;
    color: #777;
}

.back {
    display: inline-block;
    margin-bottom: 20px;
}

@media (max-width: 640px) {
    .wrap {
        padding-left: 16px;
        padding-right: 16px;
    }

    .topnav {
        align-items: flex-start;
    }

    .navlinks {
        gap: 10px;
    }

    .detail {
        padding: 20px;
    }
}
"""


# ---------------------------------------------------------------------------
# GENERATE INDIVIDUAL CHANGE PAGES
# ---------------------------------------------------------------------------


def choose_related(update, updates):
    candidates = []

    for candidate in updates:
        if not candidate["seo_worthy"]:
            continue

        score = related_score(update, candidate)

        if score >= 4:
            candidates.append((score, candidate))

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1].get("date_iso", ""),
        ),
        reverse=True,
    )

    return [candidate for _, candidate in candidates[:RELATED_LIMIT]]


def build_change_page(update, updates):
    category_html = "".join(
        f'<span class="tag">{escape(category)}</span>'
        for category in update["categories"]
    )

    important_html = (
        '<span class="urgent">Action / breaking change</span>'
        if update["important"]
        else ""
    )

    sections = update.get("sections") or {}
    source_what = sections.get("what") or sections.get("intro") or ""
    source_audience = sections.get("audience") or ""
    source_action = sections.get("action") or ""

    what_changed = smart_truncate(
        source_what or update["description"],
        900,
        max_sentences=5,
    )

    if not what_changed:
        what_changed = (
            f"Shopify published a developer changelog entry for: "
            f"{update['title']}."
        )

    audience_text = build_audience_text(
        update["title"],
        update["categories"],
        update["description"],
        source_audience,
    )

    action_text = build_action_text(
        update["title"],
        update["categories"],
        source_action,
    )

    related = choose_related(update, updates)
    related_items = [
        {
            "slug": candidate["slug"],
            "title": candidate["title"],
        }
        for candidate in related
    ]

    state_payload = {
        "template": CHANGE_PAGE_TEMPLATE_VERSION,
        "title": update["title"],
        "source_url": update["source_url"],
        "date": update["date_iso_datetime"],
        "categories": update["categories"],
        "what_changed": what_changed,
        "audience": audience_text,
        "action": action_text,
        "api_versions": update["api_versions"],
        "related": related_items,
        "seo_worthy": update["seo_worthy"],
    }

    content_hash = semantic_hash(state_payload)
    output_path = CHANGES_DIR / f"{update['slug']}.html"
    date_modified = page_modification_time(
        output_path,
        content_hash,
        update["date_iso_datetime"],
    )

    version_html = ""

    if update["api_versions"]:
        versions = ", ".join(
            escape(version)
            for version in update["api_versions"]
        )

        version_html = f"""
<div class="version-note">
<strong>Referenced API version:</strong> {versions}
</div>
"""

    related_html = ""

    if related:
        list_items = "".join(
            '<li><a href="'
            f'{SITE_URL}changes/{escape(candidate["slug"], quote=True)}.html">'
            f'{escape(candidate["title"])}</a></li>'
            for candidate in related
        )

        related_html = f"""
<section class="related">
<h2>Related Shopify developer changes</h2>
<ul class="related-list">
{list_items}
</ul>
</section>
"""

    description_for_meta = smart_truncate(
        what_changed
        or update["description"]
        or f"{update['title']} — Shopify developer change tracked by MerchantDiff.",
        155,
        max_sentences=2,
    )

    meta_description = escape(description_for_meta, quote=True)
    title_html = escape(update["title"])
    local_url_html = escape(update["local_url"], quote=True)
    source_url_html = escape(update["source_url"], quote=True)
    x_intent_html = escape(update["x_intent"], quote=True)

    robots_content = (
        "index,follow"
        if update["seo_worthy"]
        else "noindex,follow"
    )

    x_button = ""

    if update["seo_worthy"]:
        x_button = f"""
<a
    class="action-button"
    href="{x_intent_html}"
    target="_blank"
    rel="noopener noreferrer">
Post on X →
</a>
"""

    category_sentence = (
        "This Shopify update is categorized as "
        + escape(", ".join(update["categories"]))
        + "."
        if update["categories"]
        else "This entry was published in Shopify's official developer changelog."
    )

    article = {
        "@type": "Article",
        "@id": update["local_url"] + "#article",
        "headline": update["title"],
        "description": description_for_meta,
        "url": update["local_url"],
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": update["local_url"],
        },
        "author": {
            "@type": "Organization",
            "name": "MerchantDiff",
            "url": SITE_URL,
        },
        "publisher": {
            "@type": "Organization",
            "name": "MerchantDiff",
            "url": SITE_URL,
        },
        "dateModified": date_modified,
        "isAccessibleForFree": True,
        "inLanguage": "en",
        "isBasedOn": update["source_url"],
    }

    if update["date_iso_datetime"]:
        article["datePublished"] = update["date_iso_datetime"]

    if update["categories"]:
        article["articleSection"] = update["categories"]
        article["keywords"] = ", ".join(update["categories"])

    breadcrumb = {
        "@type": "BreadcrumbList",
        "@id": update["local_url"] + "#breadcrumb",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "MerchantDiff",
                "item": SITE_URL,
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Shopify changes",
                "item": SITE_URL + "updates.html",
            },
            {
                "@type": "ListItem",
                "position": 3,
                "name": update["title"],
                "item": update["local_url"],
            },
        ],
    }

    structured_data = json_ld_script(
        {
            "@context": "https://schema.org",
            "@graph": [article, breadcrumb],
        }
    )

    published_meta = ""

    if update["date_iso_datetime"]:
        published_meta = (
            '<meta property="article:published_time" '
            f'content="{escape(update["date_iso_datetime"], quote=True)}">'
        )

    date_markup = escape(update["date_display"])

    if update["date_iso_datetime"]:
        date_markup = (
            f'<time datetime="{escape(update["date_iso_datetime"], quote=True)}">'
            f'{date_markup}</time>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_html} | MerchantDiff</title>
<meta name="description" content="{meta_description}">
<meta name="robots" content="{robots_content}">
<link rel="canonical" href="{local_url_html}">
<meta property="og:type" content="article">
<meta property="og:title" content="{escape(update['title'], quote=True)}">
<meta property="og:description" content="{meta_description}">
<meta property="og:url" content="{local_url_html}">
{published_meta}
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{escape(update['title'], quote=True)}">
<meta name="twitter:description" content="{meta_description}">
{structured_data}
<style>
{COMMON_CSS}
</style>
</head>
<body>
<!-- merchantdiff-content-hash: {content_hash} -->
<!-- merchantdiff-lastmod: {date_modified} -->
<div class="wrap">
<nav class="topnav">
<a class="brand" href="{SITE_URL}">MerchantDiff</a>
<div class="navlinks">
<a href="{SITE_URL}updates.html">Shopify changes</a>
<a href="{X_URL}" target="_blank" rel="noopener noreferrer">X</a>
</div>
</nav>
<main>
<a class="back" href="{SITE_URL}updates.html">← Latest Shopify changes</a>
<div class="meta">
{date_markup}
{important_html}
</div>
<h1>{title_html}</h1>
<div class="tags">{category_html}</div>
<section class="detail">
<h2>Shopify developer change</h2>
<p>
MerchantDiff detected this entry in Shopify's official developer changelog on
<strong>{escape(update['date_display'])}</strong>.
</p>
<p>{category_sentence}</p>
<div class="insight-grid">
<div class="insight">
<h2>What changed</h2>
<p>{escape(what_changed)}</p>
</div>
<div class="insight">
<h2>Who is affected</h2>
<p>{escape(audience_text)}</p>
</div>
<div class="insight">
<h2>What action may be needed</h2>
<p>{escape(action_text)}</p>
</div>
</div>
{version_html}
<p>
Use the official Shopify entry below as the source of truth for technical
implementation details, affected APIs, migration instructions and deadlines.
</p>
<div class="source-box">
<strong>Official source</strong>
<p>
<a href="{source_url_html}" target="_blank" rel="noopener noreferrer">
Read this change on Shopify →
</a>
</p>
</div>
{related_html}
<div class="actions">{x_button}</div>
</section>
<section class="cta">
<h2>Want the actionable version?</h2>
<p>
MerchantDiff monitors Shopify API changes, deprecations, deadlines and
ecosystem updates, then turns them into developer-focused release intelligence:
what changed, who is affected and what action may be needed.
</p>
<a href="{BOOSTY_URL}" target="_blank" rel="noopener noreferrer">
Get MerchantDiff →
</a>
</section>
</main>
<footer>
Source metadata comes from the Shopify Developer Changelog. MerchantDiff is an
independent project and is not affiliated with Shopify.
</footer>
</div>
{analytics_snippet()}
</body>
</html>
"""


def generate_change_pages(updates):
    CHANGES_DIR.mkdir(parents=True, exist_ok=True)

    for update in updates:
        output_path = CHANGES_DIR / f"{update['slug']}.html"
        output_path.write_text(
            build_change_page(update, updates),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# READY TO POST ON X / UPDATES PAGE
# ---------------------------------------------------------------------------


def build_ready_section(updates):
    ready_updates = [
        update
        for update in updates[:MAX_UPDATES_ON_INDEX]
        if update["x_worthy"]
    ][:READY_TO_POST_LIMIT]

    if not ready_updates:
        return "", ready_updates

    cards = []

    for update in ready_updates:
        slug_html = escape(update["slug"], quote=True)
        priority_label = "HIGH PRIORITY" if update["important"] else "READY"

        cards.append(
            f"""
<div class="ready-item" data-post-key="{slug_html}">
<div class="ready-meta">
{escape(update['date_display'])} · {priority_label}
</div>
<div class="ready-item-title">{escape(update['title'])}</div>
<div class="ready-actions">
<a
    class="ready-button"
    href="{escape(update['x_intent'], quote=True)}"
    target="_blank"
    rel="noopener noreferrer"
    onclick="markAsPosted('{slug_html}')">
Post on X →
</a>
<a class="ready-page-link" href="changes/{slug_html}.html">
Preview MerchantDiff page
</a>
</div>
</div>
"""
        )

    section = f"""
<section class="ready-panel">
<h2>Ready to post on X</h2>
<p class="ready-intro">
Latest high-value Shopify developer changes selected automatically by
MerchantDiff. Clicking Post on X opens a prepared post and immediately removes
that item from this queue.
</p>
<div class="queue-toolbar">
<span class="queue-counter" id="ready-counter">
{len(ready_updates)} posts waiting
</span>
<button class="reset-button" type="button" onclick="resetPostedMarks()">
Reset posted marks
</button>
</div>
<div class="ready-list">
{''.join(cards)}
<div class="ready-empty" id="ready-empty-state">
<strong>Queue cleared ✓</strong>
No current high-value Shopify updates are waiting to be posted.
</div>
</div>
</section>
"""

    return section, ready_updates


def generate_updates_page(updates):
    ready_section, ready_updates = build_ready_section(updates)
    cards = []

    for update in updates[:MAX_UPDATES_ON_INDEX]:
        tags = "".join(
            f'<span class="tag">{escape(category)}</span>'
            for category in update["categories"][:5]
        )
        important = (
            '<span class="urgent">Action / breaking change</span>'
            if update["important"]
            else ""
        )

        sections = update.get("sections") or {}
        card_summary = smart_truncate(
            sections.get("what")
            or sections.get("intro")
            or update["description"],
            260,
            max_sentences=2,
        )

        summary_html = (
            f'<p class="card-summary">{escape(card_summary)}</p>'
            if card_summary
            else ""
        )

        x_button = ""

        if update["x_worthy"]:
            x_button = f"""
<a
    class="action-button"
    href="{escape(update['x_intent'], quote=True)}"
    target="_blank"
    rel="noopener noreferrer">
Post on X →
</a>
"""

        cards.append(
            f"""
<article class="card">
<div class="meta">
{escape(update['date_display'])}
{important}
</div>
<h2>
<a href="changes/{escape(update['slug'], quote=True)}.html">
{escape(update['title'])}
</a>
</h2>
<div class="tags">{tags}</div>
{summary_html}
<div class="actions">
<a class="secondary-link" href="changes/{escape(update['slug'], quote=True)}.html">
MerchantDiff page →
</a>
<a
    class="secondary-link"
    href="{escape(update['source_url'], quote=True)}"
    target="_blank"
    rel="noopener noreferrer">
Official Shopify source
</a>
{x_button}
</div>
</article>
"""
        )

    listed_updates = updates[:MAX_UPDATES_ON_INDEX]
    item_list = {
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": index,
                "name": update["title"],
                "item": update["local_url"],
            }
            for index, update in enumerate(listed_updates, start=1)
        ],
    }

    collection = {
        "@type": "CollectionPage",
        "@id": SITE_URL + "updates.html#collection",
        "name": "Latest Shopify developer changes",
        "url": SITE_URL + "updates.html",
        "description": (
            "Latest Shopify developer changelog updates, API changes, "
            "deprecations and platform updates tracked by MerchantDiff."
        ),
        "inLanguage": "en",
        "mainEntity": item_list,
    }

    breadcrumb = {
        "@type": "BreadcrumbList",
        "@id": SITE_URL + "updates.html#breadcrumb",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "MerchantDiff",
                "item": SITE_URL,
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Shopify changes",
                "item": SITE_URL + "updates.html",
            },
        ],
    }

    structured_data = json_ld_script(
        {
            "@context": "https://schema.org",
            "@graph": [collection, breadcrumb],
        }
    )

    description = (
        "Latest Shopify developer changelog updates, API changes, deprecations "
        "and platform updates tracked automatically by MerchantDiff."
    )

    updates_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Latest Shopify Developer Changes | MerchantDiff</title>
<meta name="description" content="{escape(description, quote=True)}">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{SITE_URL}updates.html">
<meta property="og:type" content="website">
<meta property="og:title" content="Latest Shopify Developer Changes | MerchantDiff">
<meta property="og:description" content="{escape(description, quote=True)}">
<meta property="og:url" content="{SITE_URL}updates.html">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Latest Shopify Developer Changes | MerchantDiff">
<meta name="twitter:description" content="{escape(description, quote=True)}">
{structured_data}
<style>
{COMMON_CSS}
</style>
</head>
<body>
<div class="wrap">
<nav class="topnav">
<a class="brand" href="{SITE_URL}">MerchantDiff</a>
<div class="navlinks">
<a href="{SITE_URL}">Home</a>
<a href="{X_URL}" target="_blank" rel="noopener noreferrer">X</a>
<a href="{BOOSTY_URL}" target="_blank" rel="noopener noreferrer">Subscribe</a>
</div>
</nav>
<header class="hero">
<h1>Latest Shopify developer changes</h1>
<p class="intro">
Automatically tracked updates from Shopify's official developer changelog.
MerchantDiff monitors API changes, deprecations, deadlines and important
ecosystem updates for Shopify developers.
</p>
</header>
{ready_section}
<main>
{''.join(cards)}
</main>
<section class="cta">
<h2>Need the developer-focused version?</h2>
<p>
MerchantDiff turns Shopify changes into weekly release intelligence: what
changed, who is affected and what action may be needed.
</p>
<a href="{BOOSTY_URL}" target="_blank" rel="noopener noreferrer">
Get MerchantDiff →
</a>
</section>
<footer>
Data source: Shopify Developer Changelog. MerchantDiff is an independent
project and is not affiliated with Shopify.
</footer>
</div>
{x_queue_script()}
{analytics_snippet()}
</body>
</html>
"""

    UPDATES_FILE.write_text(updates_html, encoding="utf-8")
    return ready_updates


# ---------------------------------------------------------------------------
# KEEP HOMEPAGE INTERNAL LINKS FRESH
# ---------------------------------------------------------------------------


def build_home_latest_section(updates):
    selected = [update for update in updates if update["seo_worthy"]][:3]

    if len(selected) < 3:
        seen = {update["slug"] for update in selected}

        for update in updates:
            if update["slug"] in seen:
                continue
            selected.append(update)
            seen.add(update["slug"])

            if len(selected) >= 3:
                break

    cards = []

    for update in selected:
        cards.append(
            f"""
          <a
            class="latest-item"
            href="changes/{escape(update['slug'], quote=True)}.html"
          >
            <div class="latest-meta">
              <span>{escape(update['date_display'])}</span>
              <span>·</span>
              <span>{escape(category_label(update))}</span>
            </div>
            <h3>{escape(update['title'])}</h3>
          </a>
"""
        )

    return f"""<!-- MERCHANTDIFF_LATEST_START -->
      <section id="latest">
        <div class="section-head">
          <h2>Latest Shopify developer changes.</h2>
          <p>
            Recent API changes, deprecations and developer updates tracked by MerchantDiff.
          </p>
        </div>
        <div class="latest-list">
{''.join(cards)}
        </div>
        <a class="latest-link" href="updates.html">
          View all Shopify developer changes →
        </a>
      </section>
<!-- MERCHANTDIFF_LATEST_END -->"""


def update_homepage(updates):
    if not INDEX_FILE.exists():
        print("Warning: index.html not found; homepage latest section was skipped.")
        return False

    text = INDEX_FILE.read_text(encoding="utf-8")
    new_section = build_home_latest_section(updates)

    marker_pattern = re.compile(
        r"<!-- MERCHANTDIFF_LATEST_START -->.*?"
        r"<!-- MERCHANTDIFF_LATEST_END -->",
        re.DOTALL,
    )

    if marker_pattern.search(text):
        updated = marker_pattern.sub(new_section, text, count=1)
    else:
        section_pattern = re.compile(
            r"<section\s+id=[\"']latest[\"'][^>]*>.*?</section>",
            re.DOTALL | re.IGNORECASE,
        )

        if not section_pattern.search(text):
            print(
                "Warning: #latest section not found in index.html; "
                "homepage update was skipped."
            )
            return False

        updated = section_pattern.sub(new_section, text, count=1)

    if updated == text:
        return False

    INDEX_FILE.write_text(updated, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# SITEMAP / ROBOTS
# ---------------------------------------------------------------------------


def page_is_indexable(html_text):
    return not re.search(
        r'<meta\s+name=["\']robots["\'][^>]*content=["\'][^"\']*noindex',
        html_text,
        re.IGNORECASE,
    )


def extract_lastmod(html_text):
    match = re.search(
        r"<!--\s*merchantdiff-lastmod:\s*([^\s]+)\s*-->",
        html_text,
    )
    return match.group(1) if match else ""


def generate_sitemap():
    entries = [
        f"<url><loc>{xml_escape(SITE_URL)}</loc></url>",
        f"<url><loc>{xml_escape(SITE_URL + 'updates.html')}</loc></url>",
    ]

    indexed_change_pages = 0

    for path in sorted(CHANGES_DIR.glob("*.html")):
        try:
            html_text = path.read_text(encoding="utf-8")
        except Exception:
            continue

        if not page_is_indexable(html_text):
            continue

        url = SITE_URL + "changes/" + path.name
        lastmod = extract_lastmod(html_text)

        if lastmod:
            entry = (
                f"<url><loc>{xml_escape(url)}</loc>"
                f"<lastmod>{xml_escape(lastmod)}</lastmod></url>"
            )
        else:
            entry = f"<url><loc>{xml_escape(url)}</loc></url>"

        entries.append(entry)
        indexed_change_pages += 1

    sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(entries)}
</urlset>
"""

    SITEMAP_FILE.write_text(sitemap_xml, encoding="utf-8")
    return indexed_change_pages


def generate_robots():
    robots = f"""User-agent: *
Allow: /

Sitemap: {SITE_URL}sitemap.xml
"""
    ROBOTS_FILE.write_text(robots, encoding="utf-8")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    xml_data = download_shopify_feed()
    updates = parse_updates(xml_data)

    if not updates:
        raise RuntimeError("Shopify changelog feed contained no usable items.")

    generate_change_pages(updates)
    ready_updates = generate_updates_page(updates)
    homepage_changed = update_homepage(updates)
    indexed_change_pages = generate_sitemap()
    generate_robots()

    print(
        f"Processed {len(updates)} Shopify updates. "
        f"Generated individual change pages. "
        f"{indexed_change_pages} change pages are present in sitemap.xml. "
        f"{len(ready_updates)} updates are available in the X queue. "
        f"Homepage latest section changed: {homepage_changed}."
    )


if __name__ == "__main__":
    main()
