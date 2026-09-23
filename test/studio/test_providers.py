"""Offline provider tests: every transport is fake; these tests cannot spend."""

import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.error import HTTPError, URLError

import pytest

from app.studio.providers import ProviderError, ReplicateProvider, load_catalog, plan_generation


TOKEN = "test-token-do-not-persist-123456789"
URL = "https://media.replicate.delivery/example/output.mp4"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"video-data"


def plan(maximum="1.50"):
    return plan_generation("google/veo-3.1", {"prompt": "A sunny cafe", "duration": 4}, maximum)


def prediction(status="starting", **extra):
    return {"id": "prediction123", "status": status,
            "model": "google/veo-3.1", "input": {"prompt": "A sunny cafe", "duration": 4}, **extra}


class Response(io.BytesIO):
    def __init__(self, body, url, headers=None):
        super().__init__(body)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        assert self.responses, "Unexpected HTTP call"
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(request)
        body = result if isinstance(result, bytes) else json.dumps(result).encode()
        return Response(body, request.full_url)


@pytest.fixture
def provider(tmp_path, monkeypatch):
    provider = ReplicateProvider(tmp_path, token=TOKEN)
    provider._opener = Opener([])
    monkeypatch.setattr("app.studio.providers.time.sleep", lambda _: None)
    return provider


def submit(provider, draft=None, response=None, budget="10"):
    draft = draft or plan()
    provider._opener = Opener([response or prediction()])
    return provider.submit(draft, draft["digest"], budget)


def test_catalog_examples_and_explicit_placeholders():
    catalog = load_catalog()
    assert {m["role"] for m in catalog["models"]} == {"broll", "presenter", "voice", "image"}
    for model in catalog["models"]:
        inputs = copy.deepcopy(model["example_inputs"])
        for key in ("avatar_id", "voice_id"):
            if inputs.get(key, "").startswith("REPLACE_WITH_"):
                inputs[key] = "valid_example_id"
        assert plan_generation(model["id"], inputs, "1")["role"] == model["role"]


def test_plan_is_dry_immutable_and_binds_exact_inputs():
    inputs = {"prompt": "A cafe"}
    one = plan_generation("google/veo-3.1", inputs, "2")
    two = plan_generation("google/veo-3.1", inputs, "2")
    inputs["prompt"] = "Changed"
    assert one["inputs"]["prompt"] == "A cafe"
    assert one["model_input_hash"] == two["model_input_hash"]
    assert one["digest"] != two["digest"]
    assert "provider-enforced" in one["cost_basis"]


@pytest.mark.parametrize("maximum", [0, -1, "NaN", "Infinity", True, "1.0000001"])
def test_invalid_money(maximum):
    with pytest.raises(ProviderError):
        plan(maximum)


@pytest.mark.parametrize("inputs", [
    {"prompt": "ok", "duration": True}, {"prompt": "ok", "duration": 99},
    {"prompt": "ok", "unknown": 1}, {"prompt": "ok", "api_key": "secret"},
    {"prompt": "Bearer secret"}, {"prompt": "r8_abcdefghijklmnop"},
    {"prompt": "ok", "image": "http://example.com/image.png"},
    {"prompt": "ok", "image": "https://127.0.0.1/image.png"},
    {"prompt": "ok", "image": "https://10.0.0.1/image.png"},
    {"prompt": "ok", "image": "https://localhost/image.png"},
    {"prompt": "ok", "image": "file:///etc/passwd"},
    {"prompt": "ok", "image": "https://user:password@example.com/image.png"},
    {"prompt": "ok", "image": "https://example.com/image.png?token=secret"},
])
def test_invalid_inputs_rejected_offline(inputs):
    with pytest.raises(ProviderError):
        plan_generation("google/veo-3.1", inputs, 1)


def test_custom_model_explicit_schema_and_version(provider):
    model_id = "someone/custom:" + "a" * 64
    with pytest.raises(ProviderError):
        plan_generation(model_id, {"script": "Hello"}, 1)
    draft = plan_generation(model_id, {"script": "Hello"}, 1, model_spec={
        "id": model_id, "role": "presenter", "input_schema": {"type": "object", "properties": {
            "script": {"type": "string"}}, "required": ["script"]}})
    submit(provider, draft)
    request = provider._opener.calls[0]
    assert request.full_url == "https://api.replicate.com/v1/predictions"
    assert json.loads(request.data) == {"version": model_id, "input": {"script": "Hello"}}


def test_approval_and_tamper_fail_before_transport(provider):
    draft = plan()
    with pytest.raises(ProviderError, match="Approval"):
        provider.submit(draft, "wrong", "10")
    draft["inputs"]["prompt"] = "Changed after review"
    with pytest.raises(ProviderError, match="Approval"):
        provider.submit(draft, draft["digest"], "10")
    assert not provider._opener.calls
    assert provider.budget_summary()["reserved_max_usd"] == "0"


def test_token_missing_does_not_reserve(tmp_path, monkeypatch):
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    provider = ReplicateProvider(tmp_path)
    draft = plan()
    with pytest.raises(ProviderError, match="REPLICATE_API_TOKEN"):
        provider.submit(draft, draft["digest"], 10)
    assert not provider.list_jobs()


def test_durable_reservation_precedes_post_and_idempotent_retry(provider):
    draft = plan()

    def response(request):
        disk = json.loads((provider.workspace / "provider-state.json").read_text())
        assert disk["jobs"][draft["plan_id"]]["status"] == "submitting"
        assert disk["jobs"][draft["plan_id"]]["reserved_max_usd"] == "1.50"
        return Response(json.dumps(prediction()).encode(), request.full_url)

    provider._opener = Opener([response])
    first = provider.submit(draft, draft["digest"], 10)
    second = provider.submit(draft, draft["digest"], 10)
    assert first == second
    assert len(provider._opener.calls) == 1
    assert provider._opener.calls[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in (provider.workspace / "provider-state.json").read_text()


def test_persistent_cap_across_provider_restarts_and_jobs(provider):
    submit(provider, plan("1.5"), budget="2")
    restarted = ReplicateProvider(provider.workspace, token=TOKEN)
    restarted._opener = Opener([])
    draft = plan("1")
    with pytest.raises(ProviderError, match="exceed"):
        restarted.submit(draft, draft["digest"], "2")
    with pytest.raises(ProviderError, match="persistent cap"):
        restarted.submit(draft, draft["digest"], "200")
    with pytest.raises(ProviderError, match="explicit approval"):
        restarted.set_budget_limit("3")
    restarted.set_budget_limit("3", approved=True)
    submit(restarted, draft, budget="3")
    assert Decimal(restarted.budget_summary()["reserved_max_usd"]) == Decimal("2.5")
    assert restarted.budget_summary()["actual_cost_usd"] is None


@pytest.mark.parametrize("failure", [URLError("secret-containing error"), b"not JSON", {"no": "id"}])
def test_ambiguous_post_never_retried_or_released(provider, failure):
    draft = plan()
    provider._opener = Opener([failure])
    job = provider.submit(draft, draft["digest"], 10)
    assert job["status"] == "unknown"
    assert job["prediction_id"] is None
    assert len(provider._opener.calls) == 1
    assert provider.submit(draft, draft["digest"], 10) == job
    assert provider.budget_summary()["reserved_max_usd"] == "1.50"
    with pytest.raises(ProviderError, match="reconciliation"):
        provider.poll(job["job_id"])
    assert "secret-containing" not in json.dumps(job)


def test_prediction_id_persists_when_output_validation_fails(provider):
    job = submit(provider, response=prediction("succeeded", output="https://evil.example/file.mp4"))
    assert job["prediction_id"] == "prediction123"
    assert job["status"] == "starting"
    assert "Prediction ID saved" in job["error"]


def test_get_retry_then_success(provider):
    job = submit(provider)
    provider._opener = Opener([URLError("failed"), prediction("processing")])
    assert provider.poll(job["job_id"])["status"] == "processing"
    assert len(provider._opener.calls) == 2
    assert all(r.get_method() == "GET" for r in provider._opener.calls)


def test_post_500_not_retried(provider):
    error = HTTPError("https://api.replicate.com/v1/predictions", 500, "failed", {}, None)
    draft = plan()
    provider._opener = Opener([error])
    assert provider.submit(draft, draft["digest"], 10)["status"] == "unknown"
    assert len(provider._opener.calls) == 1


def test_reconcile_requires_exact_model_inputs_and_keeps_reservation(provider):
    draft = plan()
    provider._opener = Opener([URLError("failed")])
    job = provider.submit(draft, draft["digest"], 10)
    provider._opener = Opener([prediction(input={"prompt": "another"})])
    with pytest.raises(ProviderError, match="does not match"):
        provider.reconcile(job["job_id"], "prediction123")
    provider._opener = Opener([prediction("succeeded", output=URL)])
    result = provider.reconcile(job["job_id"], "prediction123")
    assert result["status"] == "succeeded"
    assert provider.budget_summary()["reserved_max_usd"] == "1.50"
    assert provider._opener.calls[0].get_method() == "GET"


@pytest.mark.parametrize("url", ["http://replicate.delivery/a.mp4", "https://replicate.delivery.evil.com/a.mp4",
    "https://replicate.delivery@evil.com/a.mp4", "https://evil.replicate.delivery:8443/a.mp4",
    "https://127.0.0.1/a.mp4", "file:///etc/passwd", "https://replicate.delivery/a.mp4?token=secret"])
def test_ssrf_download_rejected_before_transport(provider, url):
    with pytest.raises(ProviderError):
        provider._request("GET", url)
    assert not provider._opener.calls


def test_redirects_not_followed_and_credentials_not_forwarded(provider):
    provider._opener = Opener([lambda request: Response(VIDEO, "https://evil.example/stolen")])
    with pytest.raises(ProviderError, match="redirects"):
        provider._request("GET", URL)
    assert len(provider._opener.calls) == 1


def test_download_safe_filename_signature_size_and_reuse(provider):
    job = submit(provider, response=prediction("succeeded", output=URL))
    provider._opener = Opener([VIDEO])
    result = provider.fetch_result(job["job_id"])
    artifact = result["files"][0]
    assert artifact["path"] == str(provider.workspace / job["job_id"] / "output-00.mp4")
    assert artifact["media_type"] == "video/mp4"
    assert artifact["bytes"] == len(VIDEO)
    assert len(artifact["sha256"]) == 64
    assert provider.fetch_result(job["job_id"])["files"] == result["files"]
    assert len(provider._opener.calls) == 1
    assert TOKEN not in (provider.workspace / "provider-state.json").read_text()


def test_bad_media_and_oversize_not_saved(provider):
    job = submit(provider, response=prediction("succeeded", output=URL))
    provider._opener = Opener([b"<html>not a video</html>"])
    with pytest.raises(ProviderError, match="media type"):
        provider.fetch_result(job["job_id"])
    assert not provider.get_job(job["job_id"])["files"]
    provider.max_download_bytes = 4
    provider._opener = Opener([VIDEO])
    with pytest.raises(ProviderError, match="size limit"):
        provider.fetch_result(job["job_id"])
    assert not provider.get_job(job["job_id"])["files"]


def test_job_path_traversal_and_symlink_rejected(provider, tmp_path):
    with pytest.raises(ProviderError, match="Unknown job"):
        provider.get_job("../../outside")
    job = submit(provider, response=prediction("succeeded", output=URL))
    outside = tmp_path / "outside"
    outside.mkdir()
    (provider.workspace / job["job_id"]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProviderError, match="symlink"):
        provider.fetch_result(job["job_id"])
    assert not list(outside.iterdir())


def test_state_symlink_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "provider-state.json").symlink_to(outside)
    with pytest.raises(ProviderError, match="symlink"):
        ReplicateProvider(workspace, token=TOKEN).list_jobs()
    assert outside.read_text() == "{}"


def test_failed_job_keeps_budget_and_omits_provider_error_secrets(provider):
    job = submit(provider, response=prediction("failed", error=TOKEN))
    assert job["status"] == "failed"
    assert TOKEN not in json.dumps(job)
    assert provider.budget_summary()["reserved_max_usd"] == "1.50"


def test_concurrent_submissions_cannot_overspend(provider):
    first = ReplicateProvider(provider.workspace, token=TOKEN)
    second = ReplicateProvider(provider.workspace, token=TOKEN)
    first._opener, second._opener = Opener([prediction()]), Opener([prediction(id="prediction456")])

    def run(client):
        draft = plan("2")
        try:
            return client.submit(draft, draft["digest"], "3")
        except ProviderError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [first, second]))
    assert sum(x is not None for x in results) == 1
    assert len(first._opener.calls) + len(second._opener.calls) == 1
    assert Decimal(provider.budget_summary()["reserved_max_usd"]) == Decimal("2")


def test_crashed_submitting_job_is_not_resubmitted(provider):
    draft = plan()
    job = submit(provider, draft)
    with provider._state() as state:
        persisted = state["jobs"][job["job_id"]]
        persisted["prediction_id"] = None
        persisted["status"] = "submitting"
    restarted = ReplicateProvider(provider.workspace, token=TOKEN)
    restarted._opener = Opener([])
    assert restarted.submit(draft, draft["digest"], 10)["status"] == "submitting"
    assert not restarted._opener.calls


def test_malformed_custom_schema_rejected_before_transport():
    for schema in ({"type": "object", "properties": []},
                   {"type": "object", "$ref": "https://evil.example/schema"},
                   {"type": "object", "properties": {"prompt": {"type": "string", "minLength": "bad"}}},
                   {"type": "object", "properties": {"api_key": {"type": "string"}}}):
        with pytest.raises(ProviderError):
            plan_generation("someone/custom", {}, 1, model_spec={"id": "someone/custom", "role": "image", "input_schema": schema})


def test_read_only_list_and_budget_do_not_contact_provider(provider):
    assert provider.list_jobs() == []
    assert provider.budget_summary()["actual_cost_usd"] is None
    assert not provider._opener.calls


def test_provider_urls_are_never_followed_for_polling(provider):
    job = submit(provider, response=prediction(urls={"get": "https://evil.example/steal"}))
    provider._opener = Opener([prediction("processing")])
    provider.poll(job["job_id"])
    assert provider._opener.calls[0].full_url == "https://api.replicate.com/v1/predictions/prediction123"


def test_nano_banana_pro_uses_reviewed_inputs_and_never_silent_fallback():
    model = next(item for item in load_catalog()["models"] if item["id"] == "google/nano-banana-pro")
    inputs = copy.deepcopy(model["example_inputs"])
    inputs["image_input"] = ["https://media.replicate.delivery/reference.jpg"]
    planned = plan_generation(model["id"], inputs, "0.15")
    assert planned["inputs"]["allow_fallback_model"] is False
    assert "safety_filter_level" not in planned["inputs"]
    for unsafe in ["/private/reference.jpg", "http://example.com/ref.jpg", "https://example.com/ref.jpg?token=hidden"]:
        with pytest.raises(ProviderError):
            plan_generation(model["id"], {**inputs, "image_input": [unsafe]}, "0.15")
    with pytest.raises(ProviderError, match="unsupported value"):
        plan_generation(model["id"], {**inputs, "allow_fallback_model": True}, "0.15")
    del inputs["allow_fallback_model"]
    with pytest.raises(ProviderError, match="required fields"):
        plan_generation(model["id"], inputs, "0.15")
