"""Verified local drafts for HQ distribution; this module never sends or publishes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def tracking_link(base_url: str, platform: str, campaign_id: str, creative_id: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The destination must be an HTTPS URL without credentials.")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(utm_source=platform, utm_medium="organic_social", utm_campaign=campaign_id, utm_content=creative_id)
    if parsed.hostname == "onelink.me" or parsed.hostname.endswith(".onelink.me"):
        query.update(pid=f"{platform}_organic", c=campaign_id, af_channel=platform, af_ad=creative_id)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def draft_digest(draft: dict) -> str:
    content = {key: value for key, value in draft.items() if key != "approval_digest"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _asset_file(directory: Path, reference: str) -> Path:
    file = (directory / reference).resolve()
    if not file.is_relative_to(directory) or not file.is_file():
        raise ValueError("A render asset is missing or outside the export directory.")
    return file


def build_handoff(
    render_dir: str | Path,
    platform: str = "instagram",
    caption: str = "",
    campaign_id: str = "slapz-content",
    landing_url: str = "https://slapz.ai/",
    ai_generated: bool = False,
    account: str = "@getslapz",
) -> dict:
    if platform not in {"instagram", "tiktok"}:
        raise ValueError("Choose instagram or tiktok for this Slapz handoff.")
    if not re.fullmatch(r"@[a-zA-Z0-9._]{1,64}", account):
        raise ValueError("Use an explicit @account handle.")
    if not campaign_id.strip() or len(campaign_id) > 120:
        raise ValueError("Use a campaign identifier of 1–120 characters.")
    if len(caption) > 2200:
        raise ValueError("The handoff caption must not exceed 2,200 characters.")
    directory = Path(render_dir).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assets = []
    for asset in manifest.get("assets", []):
        reference = asset.get("path", "")
        if not reference or Path(reference).suffix.lower() == ".zip":
            continue
        file = _asset_file(directory, reference)
        actual_hash = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual_hash != asset.get("sha256"):
            raise ValueError(f"Rendered asset changed since export: {file.name}. Render again before preparing a draft.")
        assets.append({**asset, "path": str(file.relative_to(directory)), "sha256": actual_hash})
    if not assets:
        raise ValueError("The manifest has no deliverable image or video assets.")
    creative_id = manifest.get("creative_id")
    if not creative_id:
        raise ValueError("The render manifest is missing its creative_id.")
    draft = {
        "version": 1,
        "app": "slapz",
        "campaign_id": campaign_id,
        "creative_id": creative_id,
        "format": manifest.get("format"),
        "size": manifest.get("size"),
        "platform": platform,
        "account": account,
        "status": "draft",
        "approved": False,
        "caption": caption,
        "disclosures": {"your_brand": True, "ai_generated": bool(ai_generated)},
        "assets": assets,
        "render_digest": manifest.get("project_digest"),
        "destination_url": tracking_link(landing_url, platform, campaign_id, creative_id),
        "required_before_delivery": [
            "Review the exact assets, caption, disclosure and destination account.",
            "Run HQ brand/platform validation and bind approval to this draft digest.",
            "Upload approved delivery files to the selected storage/publishing provider.",
            "Obtain explicit approval for the specific publish or send action.",
        ],
        "measurement": {
            "attribution_scope": "destination_link",
            "note": "A reused bio link cannot identify which post caused a click. Missing outcomes are unknown, not zero.",
            "services": ["appsflyer", "posthog", "revenuecat"],
        },
    }
    draft["approval_digest"] = draft_digest(draft)
    return draft
