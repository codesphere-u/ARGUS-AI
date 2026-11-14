"""Verification tests for ARGUS-AI v2.2.0 website, docs, and GEO/SEO assets."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"


def test_required_website_and_docs_files_exist() -> None:
    expected = [
        ROOT / "llms.txt",
        ROOT / "llms-full.txt",
        ROOT / "docs" / "GETTING_STARTED.md",
        ROOT / ".github" / "workflows" / "pages.yml",
        WEBSITE / "index.html",
        WEBSITE / "studio.html",
        WEBSITE / "proof.html",
        WEBSITE / "rules.html",
        WEBSITE / "docs.html",
        WEBSITE / "styles.css",
        WEBSITE / "app.js",
        WEBSITE / "favicon.svg",
        WEBSITE / "og-image.svg",
        WEBSITE / "robots.txt",
        WEBSITE / "sitemap.xml",
        WEBSITE / "manifest.webmanifest",
        WEBSITE / "llms.txt",
        WEBSITE / "llms-full.txt",
    ]
    missing = [str(p.relative_to(ROOT)) for p in expected if not p.exists()]
    assert missing == []


def test_website_palette_has_zero_pink_and_valid_meta() -> None:
    css = (WEBSITE / "styles.css").read_text(encoding="utf-8").lower()
    assert "#fffdf2" in css
    assert "#1d4ed8" in css
    assert "#b45309" in css
    for forbidden in ("#ec4899", "#f43f5e", "#db2777", "#e11d48", "hotpink", "deeppink"):
        assert forbidden not in css


def test_index_json_ld_and_version_aligned() -> None:
    html = (WEBSITE / "index.html").read_text(encoding="utf-8")
    assert "v2.2.0" in html
    assert "plausible.io/js/script.js" in html
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert "@graph" in payload
