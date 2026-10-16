import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.studio.handoff import build_handoff, draft_digest, tracking_link


def test_tracking_keeps_destination_and_binds_creative():
    url = tracking_link("https://slapz.onelink.me/abcd?deep_link_value=create#beat", "instagram", "voice", "card-02")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert query["deep_link_value"] == ["create"]
    assert query["af_ad"] == ["card-02"]
    assert query["utm_content"] == ["card-02"]
    assert parsed.fragment == "beat"


def test_handoff_verifies_bytes_and_copy_changes_digest(tmp_path):
    asset = tmp_path / "slide-01.jpg"
    asset.write_bytes(b"owned test fixture")
    manifest = {"creative_id": "voice-01", "project_digest": "content-specific", "project_schema_digest": "shared-schema", "assets": [{"path": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    draft = build_handoff(tmp_path, caption="your take pt. 1")
    assert draft["approved"] is False
    assert draft["render_digest"] == "content-specific"
    assert draft["approval_digest"] == draft_digest(draft)
    draft["caption"] = "different copy"
    assert draft["approval_digest"] != draft_digest(draft)
    asset.write_bytes(b"changed after review")
    with pytest.raises(ValueError, match="changed"):
        build_handoff(tmp_path)


def test_handoff_rejects_escaping_asset_path(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"creative_id": "x", "assets": [{"path": "../outside.jpg"}]}))
    with pytest.raises(ValueError, match="outside"):
        build_handoff(tmp_path)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "javascript:alert(1)"])
def test_bad_destinations_are_rejected(url):
    with pytest.raises(ValueError):
        tracking_link(url, "instagram", "campaign", "creative")
