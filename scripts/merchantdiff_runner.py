"""MerchantDiff runtime hardening layer.

This module keeps the generated site compatible with the existing
``update_shopify.py`` generator while applying small, testable overrides for
classification and presentation behavior. The workflow runs this file instead
of invoking ``update_shopify.py`` directly.
"""

from html import escape
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import update_shopify as md


def parse_description_sections(description):
    """Extract Shopify-authored sections without leaking adjacent headings.

    Shopify changelog entries sometimes add headings such as "Why this
    matters" or "Related docs" between/after the three sections MerchantDiff
    displays. The original parser kept appending those lines to the previous
    section, which could make "Who is affected" or "What action may be needed"
    contain unrelated text.
    """

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
        (
            re.compile(
                r"^(?:why this matters|why it matters|context|details)\s*:?\s*",
                re.IGNORECASE,
            ),
            "context",
        ),
        (
            re.compile(
                r"^(?:related docs|related documentation|documentation|"
                r"learn more|resources)\s*:?\s*",
                re.IGNORECASE,
            ),
            "docs",
        ),
    ]

    sections = {
        "intro": [],
        "what": [],
        "audience": [],
        "action": [],
        "context": [],
        "docs": [],
    }
    current = "intro"

    for raw_line in (description or "").splitlines():
        line = md.compact_text(raw_line)
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

        if not matched:
            sections[current].append(line)

    return {
        key: md.compact_text(" ".join(value))
        for key, value in sections.items()
    }


def build_audience_text(title, categories, description, source_audience=""):
    """Classify the affected audience from strong signals first.

    A bare mention of a word such as "checkout" in body copy is not enough to
    classify an Admin GraphQL change as a Checkout customization change. The
    title and Shopify categories are treated as authoritative signals; body
    text is used only for a few narrow fallbacks.
    """

    if source_audience:
        return md.smart_truncate(source_audience, 440, max_sentences=3)

    primary = " ".join(
        [title or "", " ".join(categories or [])]
    ).lower()
    body = (description or "").lower()

    def has(text, *signals):
        return any(signal in text for signal in signals)

    # Highly specific surfaces first.
    if has(primary, "pos ui extension", "pos ui extensions", "pos extensions"):
        return (
            "Developers maintaining Shopify POS apps or POS UI extensions "
            "should review this change."
        )

    if has(primary, "customer account", "customer accounts"):
        return (
            "Developers building customer account experiences or extensions "
            "should review this change."
        )

    if has(primary, "checkout ui", "checkout extension", "checkout extensions"):
        return (
            "Developers building or maintaining Shopify checkout UI "
            "extensions should review this change."
        )

    if has(
        primary,
        "packed dimension",
        "packed product dimension",
        "packeddimensions",
        "inventoryitemmeasurement",
        "inventory item measurement",
    ):
        return (
            "Apps that manage inventory item measurements, package dimensions, "
            "or shipping workflows through Shopify APIs should review this "
            "change."
        )

    if has(primary, "webhook", "webhooks", "event payload", "events payload"):
        return (
            "Apps that subscribe to or process Shopify webhooks or event "
            "payloads should review this change."
        )

    if has(primary, "shopify function", "shopify functions", "function api"):
        return "Developers building Shopify Functions should review this change."

    if has(primary, "metafield", "metafields"):
        return (
            "Apps that read, write, translate, or depend on Shopify metafields "
            "should review this change."
        )

    if has(primary, "discount", "discounts"):
        return (
            "Apps that create, manage, or depend on Shopify discount "
            "functionality should review this change."
        )

    if has(primary, "storefront api", "storefront"):
        return (
            "Apps and storefronts using the affected Storefront API surface "
            "should review this change."
        )

    if has(primary, "admin graphql api", "graphql"):
        return (
            "Apps using the affected Shopify GraphQL API surface should review "
            "this change."
        )

    if has(primary, "rest api"):
        return (
            "Apps still using the affected Shopify REST API surface should "
            "review this change."
        )

    if has(primary, "catalog"):
        return (
            "Apps and integrations using Shopify catalog APIs should review "
            "this change."
        )

    if has(primary, "access token", "access tokens", "oauth", "authentication"):
        return (
            "Apps that create, store, refresh, or validate Shopify access "
            "credentials should review this change."
        )

    if has(primary, "theme", "themes", "liquid"):
        return (
            "Theme and storefront developers using the affected Shopify "
            "surface should review this change."
        )

    if has(primary, "inventory", "shipping", "fulfillment", "fulfillments"):
        return (
            "Apps that manage Shopify inventory, shipping, or fulfillment "
            "workflows should review this change."
        )

    if has(primary, "order", "orders"):
        return (
            "Apps that read or manage Shopify order data should review this "
            "change."
        )

    # Narrow body fallbacks. Intentionally do not use a generic "checkout"
    # match here because checkout is frequently mentioned as downstream context.
    if has(
        body,
        "inventoryitemmeasurement",
        "inventory item measurement",
        "packed dimensions",
    ):
        return (
            "Apps that manage inventory item measurements, package dimensions, "
            "or shipping workflows through Shopify APIs should review this "
            "change."
        )

    if has(body, "shopify functions"):
        return "Developers building Shopify Functions should review this change."

    return (
        "Shopify app developers using the API or platform feature named in "
        "this update should review the official change."
    )


def build_ready_section(updates):
    """Render the X queue with explicit browser-local state semantics."""

    ready_updates = [
        update
        for update in updates[: md.MAX_UPDATES_ON_INDEX]
        if update["x_worthy"]
    ][: md.READY_TO_POST_LIMIT]

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
MerchantDiff. Clicking Post on X opens a prepared post and hides that item in
this browser. MerchantDiff cannot verify whether the post was actually
published on X.
</p>
<div class="queue-toolbar">
<span class="queue-counter" id="ready-counter">
{len(ready_updates)} posts waiting
</span>
<button class="reset-button" type="button" onclick="resetPostedMarks()">
Reset browser-local posted marks
</button>
</div>
<p style="margin:-8px 0 18px;color:#9b9b9b;font-size:12px;line-height:1.5;">
Posted marks are stored in this browser only, so another browser or device can
show the same candidates again.
</p>
<div class="ready-list">
{''.join(cards)}
<div class="ready-empty" id="ready-empty-state">
<strong>Queue cleared in this browser ✓</strong>
No current high-value Shopify updates are visible in this browser-local queue.
</div>
</div>
</section>
"""

    return section, ready_updates


def validate_outputs():
    """Fail the workflow when core generated artifacts are malformed."""

    if not md.UPDATES_FILE.exists():
        raise RuntimeError("updates.html was not generated")
    if not md.SITEMAP_FILE.exists():
        raise RuntimeError("sitemap.xml was not generated")
    if not md.ROBOTS_FILE.exists():
        raise RuntimeError("robots.txt was not generated")

    root = ET.fromstring(md.SITEMAP_FILE.read_text(encoding="utf-8"))
    if not root.tag.endswith("urlset"):
        raise RuntimeError("sitemap.xml root is not <urlset>")

    robots = md.ROBOTS_FILE.read_text(encoding="utf-8")
    expected_sitemap = f"Sitemap: {md.SITE_URL}sitemap.xml"
    if expected_sitemap not in robots:
        raise RuntimeError("robots.txt does not reference MerchantDiff sitemap")

    print("MerchantDiff output validation passed.")


def apply_overrides():
    md.parse_description_sections = parse_description_sections
    md.build_audience_text = build_audience_text
    md.build_ready_section = build_ready_section


def main():
    apply_overrides()
    md.main()
    validate_outputs()


if __name__ == "__main__":
    main()
