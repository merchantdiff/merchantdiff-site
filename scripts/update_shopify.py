from urllib.request import Request, urlopen
from urllib.parse import urlparse, urlencode
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape
import os
import re
import hashlib


FEED_URL = "https://shopify.dev/changelog/feed.xml"

SITE_URL = "https://merchantdiff.github.io/merchantdiff-site/"
BOOSTY_URL = "https://boosty.to/merchantdiff"
X_URL = "https://x.com/MerchantDiff"

ANALYTICS_TOKEN = "44ece3bc3eee498c9bed2bbfd20a997c"

CHANGES_DIR = "changes"

MAX_FEED_ITEMS = 100
MAX_UPDATES_ON_INDEX = 30

# Сколько важных обновлений показываем
# в отдельном блоке Ready to post on X
READY_TO_POST_LIMIT = 5


def safe_slug(text):
    text = text.lower().strip()
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


def parse_date(pub_date):
    try:
        parsed = parsedate_to_datetime(pub_date)

        return {
            "display": parsed.strftime("%B %d, %Y"),
            "iso": parsed.strftime("%Y-%m-%d"),
        }

    except Exception:
        return {
            "display": pub_date,
            "iso": "",
        }


def is_important(title, categories):
    values = " ".join(categories).lower()
    title_lower = title.lower()

    important_signals = [
        "action required",
        "breaking api change",
        "breaking change",
        "breaking changes",
        "deprecation announcement",
        "deprecated",
        "deprecation",
    ]

    return any(
        signal in values or signal in title_lower
        for signal in important_signals
    )


def is_seo_worthy(title, categories):
    category_text = " ".join(categories).lower()
    title_text = title.lower()

    strong_category_signals = [
        "action required",
        "breaking api change",
        "breaking change",
        "deprecation announcement",
        "deprecation",
    ]

    strong_title_signals = [
        "deprecated",
        "deprecation",
        "breaking",
        "removed",
        "removal",
        "sunset",
        "deadline",
        "action required",
        "no longer supported",
        "no longer available",
        "api version",
        "migration required",
    ]

    technical_title_signals = [
        "graphql",
        "rest api",
        "webhook",
        "checkout",
        "shopify functions",
        "access token",
    ]

    technical_change_signals = [
        "change",
        "update",
        "new",
        "removed",
        "deprecated",
        "require",
    ]

    strong_category_match = any(
        signal in category_text
        for signal in strong_category_signals
    )

    strong_title_match = any(
        signal in title_text
        for signal in strong_title_signals
    )

    technical_match = (
        any(
            signal in title_text
            for signal in technical_title_signals
        )
        and any(
            signal in title_text
            for signal in technical_change_signals
        )
    )

    return (
        strong_category_match
        or strong_title_match
        or technical_match
    )


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
    title = title.strip()

    if len(title) > 155:
        title = title[:152].rstrip() + "..."

    if important:
        intro = "Important Shopify developer change:"
    else:
        intro = "Shopify developer update:"

    return (
        f"{intro}\n\n"
        f"{title}\n\n"
        f"{local_url}\n\n"
        "#ShopifyDev"
    )


def build_x_intent(post_text):
    query = urlencode(
        {
            "text": post_text,
        }
    )

    return f"https://x.com/intent/tweet?{query}"


# ---------------------------------------------------------
# DOWNLOAD SHOPIFY CHANGELOG
# ---------------------------------------------------------

request = Request(
    FEED_URL,
    headers={
        "User-Agent":
            "MerchantDiff/1.0 "
            "(+https://merchantdiff.github.io/merchantdiff-site/)"
    },
)

with urlopen(request, timeout=30) as response:
    xml_data = response.read()


# ---------------------------------------------------------
# PARSE FEED
# ---------------------------------------------------------

root = ET.fromstring(xml_data)

items = root.findall(".//item")[:MAX_FEED_ITEMS]

updates = []

for item in items:
    title = (
        item.findtext("title")
        or ""
    ).strip()

    link = (
        item.findtext("link")
        or item.findtext("guid")
        or ""
    ).strip()

    pub_date = (
        item.findtext("pubDate")
        or ""
    ).strip()

    if not title or not link:
        continue

    categories = [
        (category.text or "").strip()
        for category in item.findall("category")
        if category.text
    ]

    date = parse_date(pub_date)

    slug = page_slug(
        title,
        link,
    )

    local_url = (
        f"{SITE_URL}changes/{slug}.html"
    )

    important = is_important(
        title,
        categories,
    )

    seo_worthy = is_seo_worthy(
        title,
        categories,
    )

    x_post = build_x_post(
        title,
        local_url,
        important,
    )

    x_intent = build_x_intent(
        x_post
    )

    updates.append(
        {
            "title": title,
            "source_url": link,
            "date_display": date["display"],
            "date_iso": date["iso"],
            "categories": categories,
            "important": important,
            "seo_worthy": seo_worthy,
            "slug": slug,
            "local_url": local_url,
            "x_post": x_post,
            "x_intent": x_intent,
        }
    )


# ---------------------------------------------------------
# CREATE CHANGES DIRECTORY
# ---------------------------------------------------------

os.makedirs(
    CHANGES_DIR,
    exist_ok=True,
)


# ---------------------------------------------------------
# COMMON CSS
# ---------------------------------------------------------

COMMON_CSS = """
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
    background: #f7f8fa;
    color: #161616;
}

a {
    color: #1457d9;
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
    margin: 0 0 22px;
    max-width: 700px;
    color: #cfcfcf;
    line-height: 1.55;
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
"""


# ---------------------------------------------------------
# GENERATE INDIVIDUAL SHOPIFY CHANGE PAGES
# ---------------------------------------------------------

for update in updates:

    category_html = "".join(
        f'<span class="tag">{escape(category)}</span>'
        for category in update["categories"]
    )

    important_html = (
        '<span class="urgent">'
        'Action / breaking change'
        '</span>'
        if update["important"]
        else ""
    )

    title = escape(
        update["title"]
    )

    source_url = escape(
        update["source_url"]
    )

    local_url = escape(
        update["local_url"]
    )

    x_intent = escape(
        update["x_intent"],
        quote=True,
    )

    categories_text = ", ".join(
        update["categories"]
    )

    if categories_text:
        category_sentence = (
            "This Shopify update is categorized as "
            f"{escape(categories_text)}."
        )

    else:
        category_sentence = (
            "This entry was published in Shopify's "
            "official developer changelog."
        )

    meta_description = (
        f"{update['title']} — Shopify developer change "
        f"tracked by MerchantDiff. "
        f"Published {update['date_display']}."
    )

    meta_description = escape(
        meta_description[:155]
    )

    robots_meta = (
        ""
        if update["seo_worthy"]
        else (
            '<meta name="robots" '
            'content="noindex,follow">'
        )
    )

    x_button = ""

    if update["seo_worthy"]:
        x_button = f"""
<a
    class="action-button"
    href="{x_intent}"
    target="_blank"
    rel="noopener noreferrer">
Post on X →
</a>
"""

    page_html = f"""<!doctype html>
<html lang="en">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1">

<title>{title} | MerchantDiff</title>

<meta
    name="description"
    content="{meta_description}">

<link
    rel="canonical"
    href="{local_url}">

{robots_meta}

<meta property="og:type" content="article">

<meta
    property="og:title"
    content="{title}">

<meta
    property="og:url"
    content="{local_url}">

<style>
{COMMON_CSS}
</style>

</head>

<body>

<div class="wrap">

<nav class="topnav">

<a
    class="brand"
    href="{SITE_URL}">
MerchantDiff
</a>

<div class="navlinks">

<a href="{SITE_URL}updates.html">
Shopify changes
</a>

<a
    href="{X_URL}"
    target="_blank"
    rel="noopener">
X
</a>

</div>

</nav>

<main>

<a
    class="back"
    href="{SITE_URL}updates.html">
← Latest Shopify changes
</a>

<div class="meta">

{escape(update["date_display"])}

{important_html}

</div>

<h1>{title}</h1>

<div class="tags">
{category_html}
</div>

<section class="detail">

<h2>Shopify developer change</h2>

<p>
MerchantDiff detected this entry in Shopify's official
developer changelog on
<strong>{escape(update["date_display"])}</strong>.
</p>

<p>
{category_sentence}
</p>

<p>
Use the official Shopify entry below as the source of truth
for technical implementation details, affected APIs,
migration instructions and deadlines.
</p>

<div class="source-box">

<strong>Official source</strong>

<p>

<a
    href="{source_url}"
    target="_blank"
    rel="noopener noreferrer">
Read this change on Shopify →
</a>

</p>

</div>

<div class="actions">

{x_button}

</div>

</section>

<section class="cta">

<h2>Want the actionable version?</h2>

<p>
MerchantDiff monitors Shopify API changes, deprecations,
deadlines and ecosystem updates, then turns them into
developer-focused release intelligence: what changed,
who is affected and what action may be needed.
</p>

<a
    href="{BOOSTY_URL}"
    target="_blank"
    rel="noopener noreferrer">
Get MerchantDiff →
</a>

</section>

</main>

<footer>

Source metadata comes from the Shopify Developer Changelog.
MerchantDiff is an independent project and is not affiliated
with Shopify.

</footer>

</div>

{analytics_snippet()}

</body>
</html>
"""

    output_path = os.path.join(
        CHANGES_DIR,
        f"{update['slug']}.html",
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(
            page_html
        )


# ---------------------------------------------------------
# READY TO POST ON X
# ---------------------------------------------------------

ready_updates = [
    update
    for update in updates[:MAX_UPDATES_ON_INDEX]
    if update["seo_worthy"]
][:READY_TO_POST_LIMIT]

ready_cards = []

for update in ready_updates:

    x_intent = escape(
        update["x_intent"],
        quote=True,
    )

    priority_label = (
        "HIGH PRIORITY"
        if update["important"]
        else "READY"
    )

    ready_cards.append(
        f"""
<div class="ready-item">

<div class="ready-meta">
{escape(update["date_display"])} · {priority_label}
</div>

<div class="ready-item-title">
{escape(update["title"])}
</div>

<div class="ready-actions">

<a
    class="ready-button"
    href="{x_intent}"
    target="_blank"
    rel="noopener noreferrer">
Post on X →
</a>

<a
    class="ready-page-link"
    href="changes/{escape(update["slug"])}.html">
Preview MerchantDiff page
</a>

</div>

</div>
"""
    )


if ready_cards:
    ready_section = f"""
<section class="ready-panel">

<h2>Ready to post on X</h2>

<p class="ready-intro">
Latest high-value Shopify developer changes selected
automatically by MerchantDiff. Clicking the button opens
X with a prepared post — nothing is published until you
press Post.
</p>

<div class="ready-list">

{''.join(ready_cards)}

</div>

</section>
"""

else:
    ready_section = ""


# ---------------------------------------------------------
# GENERATE updates.html
# ---------------------------------------------------------

cards = []

for update in updates[:MAX_UPDATES_ON_INDEX]:

    tags = "".join(
        f'<span class="tag">{escape(category)}</span>'
        for category in update["categories"][:5]
    )

    important = (
        '<span class="urgent">'
        'Action / breaking change'
        '</span>'
        if update["important"]
        else ""
    )

    x_button = ""

    if update["seo_worthy"]:
        x_intent = escape(
            update["x_intent"],
            quote=True,
        )

        x_button = f"""
<a
    class="action-button"
    href="{x_intent}"
    target="_blank"
    rel="noopener noreferrer">
Post on X →
</a>
"""

    cards.append(
        f"""
<article class="card">

<div class="meta">
{escape(update["date_display"])}
{important}
</div>

<h2>

<a href="changes/{escape(update["slug"])}.html">
{escape(update["title"])}
</a>

</h2>

<div class="tags">
{tags}
</div>

<div class="actions">

<a
    class="secondary-link"
    href="changes/{escape(update["slug"])}.html">
MerchantDiff page →
</a>

<a
    class="secondary-link"
    href="{escape(update["source_url"])}"
    target="_blank"
    rel="noopener noreferrer">
Official Shopify source
</a>

{x_button}

</div>

</article>
"""
    )


updates_html = f"""<!doctype html>
<html lang="en">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1">

<title>
Latest Shopify Developer Changes | MerchantDiff
</title>

<meta
    name="description"
    content="Latest Shopify developer changelog updates, API changes, deprecations and platform updates tracked automatically by MerchantDiff.">

<link
    rel="canonical"
    href="{SITE_URL}updates.html">

<style>
{COMMON_CSS}
</style>

</head>

<body>

<div class="wrap">

<nav class="topnav">

<a
    class="brand"
    href="{SITE_URL}">
MerchantDiff
</a>

<div class="navlinks">

<a
    href="{X_URL}"
    target="_blank"
    rel="noopener">
X
</a>

<a
    href="{BOOSTY_URL}"
    target="_blank"
    rel="noopener">
Subscribe
</a>

</div>

</nav>

<header class="hero">

<h1>
Latest Shopify developer changes
</h1>

<p class="intro">
Automatically tracked updates from Shopify's official
developer changelog. MerchantDiff monitors API changes,
deprecations, deadlines and important ecosystem updates
for Shopify developers.
</p>

</header>

{ready_section}

<main>

{''.join(cards)}

</main>

<section class="cta">

<h2>
Need the developer-focused version?
</h2>

<p>
MerchantDiff turns Shopify changes into weekly release
intelligence: what changed, who is affected and what
action may be needed.
</p>

<a
    href="{BOOSTY_URL}"
    target="_blank"
    rel="noopener noreferrer">
Get MerchantDiff →
</a>

</section>

<footer>

Data source: Shopify Developer Changelog.
MerchantDiff is an independent project and is not
affiliated with Shopify.

</footer>

</div>

{analytics_snippet()}

</body>
</html>
"""


with open(
    "updates.html",
    "w",
    encoding="utf-8",
) as file:
    file.write(
        updates_html
    )


# ---------------------------------------------------------
# SELECT IMPORTANT PAGES FOR SEARCH INDEXING
# ---------------------------------------------------------

seo_pages = [
    update
    for update in updates
    if update["seo_worthy"]
]


# ---------------------------------------------------------
# GENERATE sitemap.xml
# ---------------------------------------------------------

sitemap_entries = [
    f"""
<url>
<loc>{SITE_URL}</loc>
<changefreq>weekly</changefreq>
<priority>1.0</priority>
</url>
""",
    f"""
<url>
<loc>{SITE_URL}updates.html</loc>
<changefreq>daily</changefreq>
<priority>0.9</priority>
</url>
""",
]


for update in seo_pages:

    sitemap_entries.append(
        f"""
<url>
<loc>{escape(update["local_url"])}</loc>
<changefreq>monthly</changefreq>
<priority>0.7</priority>
</url>
"""
    )


sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>

<urlset
    xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">

{''.join(sitemap_entries)}

</urlset>
"""


with open(
    "sitemap.xml",
    "w",
    encoding="utf-8",
) as file:
    file.write(
        sitemap_xml
    )


print(
    f"Processed {len(updates)} Shopify updates. "
    f"Generated individual change pages. "
    f"{len(seo_pages)} pages selected for search indexing. "
    f"{len(ready_updates)} updates ready to post on X."
)
