from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape

FEED_URL = "https://shopify.dev/changelog/feed.xml"
SITE_URL = "https://merchantdiff.github.io/merchantdiff-site/"
BOOSTY_URL = "https://boosty.to/merchantdiff"

request = Request(
    FEED_URL,
    headers={"User-Agent": "MerchantDiff/1.0"}
)

with urlopen(request, timeout=30) as response:
    xml_data = response.read()

root = ET.fromstring(xml_data)
items = root.findall(".//item")

updates = []

for item in items[:20]:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or item.findtext("guid") or "").strip()
    pub_date = (item.findtext("pubDate") or "").strip()

    categories = [
        (category.text or "").strip()
        for category in item.findall("category")
        if category.text
    ]

    try:
        date = parsedate_to_datetime(pub_date).strftime("%B %d, %Y")
    except Exception:
        date = pub_date

    important_categories = {
        "Action Required",
        "Breaking API Change",
        "Deprecation Announcement",
    }

    urgent = any(category in important_categories for category in categories)

    updates.append({
        "title": title,
        "link": link,
        "date": date,
        "categories": categories,
        "urgent": urgent,
    })

cards = []

for update in updates:
    tags = "".join(
        f'<span class="tag">{escape(category)}</span>'
        for category in update["categories"][:4]
    )

    urgent = (
        '<span class="urgent">Action / breaking change</span>'
        if update["urgent"]
        else ""
    )

    cards.append(f"""
    <article class="card">
        <div class="meta">
            <span>{escape(update["date"])}</span>
            {urgent}
        </div>

        <h2>
            <a href="{escape(update["link"])}"
               target="_blank"
               rel="noopener noreferrer">
                {escape(update["title"])}
            </a>
        </h2>

        <div class="tags">{tags}</div>

        <a class="source"
           href="{escape(update["link"])}"
           target="_blank"
           rel="noopener noreferrer">
            Read on Shopify →
        </a>
    </article>
    """)

html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Latest Shopify Developer Changes | MerchantDiff</title>

<meta name="description"
content="Latest Shopify developer changelog updates, API changes, deprecations and platform updates tracked by MerchantDiff.">

<link rel="canonical"
href="{SITE_URL}updates.html">

<style>
body {{
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f7f8fa;
    color: #161616;
}}

.wrap {{
    max-width: 900px;
    margin: auto;
    padding: 32px 20px 60px;
}}

header {{
    margin-bottom: 32px;
}}

.brand {{
    font-weight: 700;
    font-size: 20px;
}}

h1 {{
    font-size: 42px;
    margin-bottom: 12px;
}}

.intro {{
    font-size: 18px;
    line-height: 1.6;
    color: #555;
}}

.card {{
    background: white;
    border: 1px solid #e5e5e5;
    border-radius: 14px;
    padding: 22px;
    margin: 16px 0;
}}

.card h2 {{
    margin: 12px 0;
    font-size: 22px;
}}

.card h2 a {{
    color: #161616;
    text-decoration: none;
}}

.card h2 a:hover {{
    text-decoration: underline;
}}

.meta {{
    font-size: 14px;
    color: #666;
}}

.tag {{
    display: inline-block;
    margin: 4px 6px 4px 0;
    padding: 5px 9px;
    border-radius: 20px;
    background: #eef1f5;
    font-size: 12px;
}}

.urgent {{
    display: inline-block;
    margin-left: 10px;
    font-weight: 700;
}}

.source {{
    display: inline-block;
    margin-top: 12px;
}}

.cta {{
    margin-top: 40px;
    padding: 28px;
    background: #161616;
    color: white;
    border-radius: 14px;
}}

.cta a {{
    color: white;
}}

footer {{
    margin-top: 40px;
    font-size: 14px;
    color: #777;
}}
</style>
</head>

<body>
<div class="wrap">

<header>
    <div class="brand">
        <a href="{SITE_URL}">MerchantDiff</a>
    </div>

    <h1>Latest Shopify developer changes</h1>

    <p class="intro">
        Automatically tracked updates from Shopify's official developer changelog.
        MerchantDiff monitors API changes, deprecations, deadlines and important
        ecosystem updates for Shopify developers.
    </p>
</header>

<main>
{''.join(cards)}
</main>

<section class="cta">
    <h2>Need the developer-focused version?</h2>

    <p>
        MerchantDiff turns Shopify changes into weekly release intelligence:
        what changed, who is affected and what action may be needed.
    </p>

    <a href="{BOOSTY_URL}" target="_blank" rel="noopener">
        Get MerchantDiff →
    </a>
</section>

<footer>
    Data source: Shopify Developer Changelog.
    MerchantDiff is an independent project and is not affiliated with Shopify.
</footer>

</div>

<!-- Cloudflare Web Analytics -->
<script type="module"
src="https://static.cloudflareinsights.com/beacon.min.js"
data-cf-beacon='{{"token":"44ece3bc3eee498c9bed2bbfd20a997c"}}'>
</script>
<!-- End Cloudflare Web Analytics -->

</body>
</html>
"""

with open("updates.html", "w", encoding="utf-8") as file:
    file.write(html)

print(f"Generated updates.html with {len(updates)} Shopify updates.")
