"""Replicate media jobs with local approvals, durable reservations and no POST retries.

Only ``submit`` creates paid predictions. Planning, polling and retrieving are
separate actions. Reservations are caller-declared allowances, NOT a guarantee
about a provider invoice; no actual cost is inferred from runtime metrics.
HTTP reference: https://replicate.com/docs/reference/http (checked 2026-09-22).
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ProviderError(ValueError):
    """Safe, user-facing failure. Never includes raw provider responses or tokens."""


class _HTTPFailure(ProviderError):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"Provider HTTP {status}" if status else "Provider connection failed")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}/[a-z0-9][a-z0-9._-]{0,99}(?::[0-9a-f]{64})?\Z")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_JOB = re.compile(r"[0-9a-f]{32}\Z")
_SECRET_KEY = re.compile(r"(?:^|_)(?:token|secret|password|authorization|api_key|credential)(?:$|_)", re.I)
_TOKEN = re.compile(r"\br8_[A-Za-z0-9]{10,}\b")
_ROLES = {"broll", "presenter", "voice", "image"}
_TERMINAL = {"succeeded", "failed", "canceled"}
_STATUS = _TERMINAL | {"starting", "processing"}
_API = "https://api.replicate.com/v1"
_MAX_JSON = 2 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("Expected finite, JSON-serializable values") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 100000 or amount.as_tuple().exponent < -6:
            raise InvalidOperation
        return format(amount, "f")
    except (InvalidOperation, ValueError, TypeError):
        raise ProviderError("USD amount must be positive, finite and have at most six decimal places") from None


def _https_url(value, *, delivery=False):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ProviderError("Expected a safe HTTPS URL")
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.fragment:
            raise ValueError
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or any(not label for label in host.split(".")):
            raise ValueError
        if delivery:
            if not (host == "replicate.delivery" or host.endswith(".replicate.delivery")):
                raise ValueError
        else:
            if "." not in host or host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
                raise ValueError
            try:
                if not ipaddress.ip_address(host).is_global:
                    raise ValueError
            except ValueError:
                # A numeric host must be a valid globally routable IP address.
                if re.fullmatch(r"[0-9.]+", host):
                    raise ValueError
        # Signed/credential-bearing URLs are deliberately not serialized in jobs.
        if url.query:
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("Use an uncredentialed public HTTPS URL" + (" on replicate.delivery" if delivery else "")) from None
    return value


def _safe_json(value, depth=0):
    if depth > 8:
        raise ProviderError("Input JSON is nested too deeply")
    if isinstance(value, dict):
        if len(value) > 100:
            raise ProviderError("Too many input fields")
        for key, item in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,99}", key) or _SECRET_KEY.search(key):
                raise ProviderError("Input keys must be simple names and must not contain credentials")
            _safe_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ProviderError("Too many input items")
        for item in value:
            _safe_json(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 20000 or _TOKEN.search(value) or "Bearer " in value:
            raise ProviderError("Input is too long or contains a credential")
        if value.lower().startswith(("http:", "https:", "file:", "data:", "ftp:")):
            _https_url(value)
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ProviderError("Inputs must contain JSON values only")
    if isinstance(value, float) and not math.isfinite(value):
        raise ProviderError("Input numbers must be finite")


def _validate(value, schema, name="inputs"):
    kind = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": isinstance(value, bool),
               "integer": isinstance(value, int) and not isinstance(value, bool),
               "number": isinstance(value, (int, float)) and not isinstance(value, bool),
               "json": True}
    if kind not in matches or not matches[kind]:
        raise ProviderError(f"{name}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ProviderError(f"{name}: unsupported value")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(value) - set(properties) and not schema.get("additionalProperties", False):
            raise ProviderError(f"{name}: unsupported input fields")
        if set(schema.get("required", [])) - set(value):
            raise ProviderError(f"{name}: required fields are missing")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{name}.{key}")
    elif kind == "array":
        if len(value) > schema.get("maxItems", 10):
            raise ProviderError(f"{name}: too many items")
        for item in value:
            _validate(item, schema.get("items", {"type": "json"}), name)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 20000):
            raise ProviderError(f"{name}: invalid text length")
        if value.startswith("REPLACE_WITH_"):
            raise ProviderError(f"{name}: replace the example placeholder")
        if schema.get("format") == "https-url":
            _https_url(value)
    elif kind in {"integer", "number"}:
        if not schema.get("minimum", -1e100) <= value <= schema.get("maximum", 1e100):
            raise ProviderError(f"{name}: out of range")


def _check_schema(schema, depth=0):
    """Deliberately small JSON-schema subset; no refs, loaders or executable hooks."""
    if depth > 8 or not isinstance(schema, dict):
        raise ProviderError("Invalid input schema")
    kind = schema.get("type")
    allowed = {"type", "properties", "required", "additionalProperties", "items", "maxItems",
               "enum", "minimum", "maximum", "minLength", "maxLength", "format"}
    if kind not in {"object", "array", "string", "boolean", "integer", "number", "json"} or set(schema) - allowed:
        raise ProviderError("Unsupported input schema feature")
    for key in ("minimum", "maximum", "minLength", "maxLength", "maxItems"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))):
            raise ProviderError("Input schema bounds must be numbers")
    if "enum" in schema and (not isinstance(schema["enum"], list) or len(schema["enum"]) > 100):
        raise ProviderError("Input schema enum must be a short list")
    if schema.get("format") not in (None, "https-url"):
        raise ProviderError("Only https-url input format is supported")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or len(properties) > 100 or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or set(required) - set(properties) or not isinstance(schema.get("additionalProperties", False), bool)):
            raise ProviderError("Invalid object input schema")
        for key, child in properties.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,99}", key) or _SECRET_KEY.search(key):
                raise ProviderError("Schema fields cannot be credentials")
            _check_schema(child, depth + 1)
    elif kind == "array":
        _check_schema(schema.get("items", {"type": "json"}), depth + 1)


def load_catalog():
    return json.loads(Path(__file__).with_name("catalog.json").read_text())


def plan_generation(model_id, inputs, max_cost_usd, *, role=None, model_spec=None):
    """Build a dry-run plan. Custom specs require an explicit role and input schema.

    Example custom spec: {"id":"owner/model:64hex", "role":"broll",
    "input_schema":{"type":"object", "properties":{"prompt":{"type":"string"}},
    "required":["prompt"]}}. Inputs are passed by their exact provider field names.
    """
    if not isinstance(model_id, str) or not _MODEL.fullmatch(model_id):
        raise ProviderError("Model must be owner/name with an optional full 64-hex version")
    entry = next((x for x in load_catalog()["models"] if x["id"] == model_id), None)
    if entry is None:
        if not isinstance(model_spec, dict) or model_spec.get("id") != model_id:
            raise ProviderError("Choose a curated model or supply an explicit model spec")
        entry = model_spec
    role = role or entry.get("role")
    if role not in _ROLES or role != entry.get("role"):
        raise ProviderError("Model role must be broll, presenter, voice or image")
    schema = entry.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ProviderError("Model spec needs an object input_schema")
    if len(_json(schema).encode()) > 100000:
        raise ProviderError("Input schema is too large")
    _check_schema(schema)
    if not isinstance(inputs, dict) or len(_json(inputs).encode()) > 100000:
        raise ProviderError("Inputs must be a JSON object under 100 KB")
    _safe_json(inputs)
    _validate(inputs, schema)
    # Clone caller-owned values so later UI edits cannot silently change approval.
    plan = json.loads(_json({"plan_id": uuid.uuid4().hex, "created_at": _now(),
        "provider": "replicate", "model_id": model_id, "role": role,
        "inputs": inputs, "input_schema": schema, "max_cost_usd": _money(max_cost_usd),
        "cost_basis": "Caller-declared reservation; actual provider charge unknown; not a provider-enforced dollar cap",
        "model_input_hash": _hash({"model_id": model_id, "inputs": inputs})}))
    plan["digest"] = _hash(plan)
    return plan


class ReplicateProvider:
    def __init__(self, workspace, token=None, *, timeout=30, get_retries=2,
                 max_download_bytes=100 * 1024 * 1024):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token = token if token is not None else os.environ.get("REPLICATE_API_TOKEN", "")
        if not isinstance(self.token, str) or any(c.isspace() for c in self.token):
            raise ProviderError("Invalid Replicate token")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120
                or isinstance(get_retries, bool) or not isinstance(get_retries, int) or not 0 <= get_retries <= 4
                or isinstance(max_download_bytes, bool) or not isinstance(max_download_bytes, int)
                or not 1 <= max_download_bytes <= 500 * 1024 * 1024):
            raise ProviderError("Invalid HTTP or download limits")
        self.timeout, self.get_retries, self.max_download_bytes = timeout, get_retries, max_download_bytes
        # Do not use ambient proxies or follow redirects with Authorization.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @contextlib.contextmanager
    def _state(self):
        path = self.workspace / "provider-state.json"
        lock_path = self.workspace / "provider-state.lock"
        if path.is_symlink() or lock_path.is_symlink():
            raise ProviderError("Provider state must not be a symlink")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if path.exists():
                    if path.stat().st_size > 32 * 1024 * 1024:
                        raise ProviderError("Provider state exceeds its size limit")
                    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))) as source:
                        state = json.load(source)
                else:
                    state = {"version": 1, "budget_limit_usd": None, "jobs": {}}
                yield state
                data = _json(state)
                if self.token and self.token in data:
                    raise ProviderError("Refusing to persist a credential")
                with tempfile.NamedTemporaryFile(mode="w", dir=self.workspace, prefix=".state-", delete=False) as out:
                    temporary = out.name
                    try:
                        os.chmod(temporary, 0o600)
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                        os.replace(temporary, path)
                        if os.name != "nt":
                            directory_fd = os.open(self.workspace, os.O_RDONLY)
                            try:
                                os.fsync(directory_fd)
                            finally:
                                os.close(directory_fd)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _job(self, state, job_id):
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id) or job_id not in state["jobs"]:
            raise ProviderError("Unknown job ID")
        return state["jobs"][job_id]

    def get_job(self, job_id):
        with self._state() as state:
            return json.loads(_json(self._job(state, job_id)))

    def list_jobs(self):
        with self._state() as state:
            return sorted(json.loads(_json(list(state["jobs"].values()))), key=lambda j: j["created_at"], reverse=True)

    def budget_summary(self):
        with self._state() as state:
            total = sum((Decimal(j["reserved_max_usd"]) for j in state["jobs"].values()), Decimal(0))
            return {"budget_limit_usd": state["budget_limit_usd"], "reserved_max_usd": str(total),
                    "actual_cost_usd": None, "note": "All reservations retained, including failed and unknown jobs; actual billing must be checked with the provider."}

    def set_budget_limit(self, amount, *, approved=False):
        if approved is not True:
            raise ProviderError("Changing the persistent budget requires explicit approval")
        amount = _money(amount)
        with self._state() as state:
            total = sum((Decimal(j["reserved_max_usd"]) for j in state["jobs"].values()), Decimal(0))
            if Decimal(amount) < total:
                raise ProviderError("Budget cannot be lower than existing reservations")
            state["budget_limit_usd"] = amount
        return self.budget_summary()

    def _require_token(self):
        if not self.token:
            raise ProviderError("Set REPLICATE_API_TOKEN in the process environment before contacting Replicate")

    def _request(self, method, url, *, payload=None, maximum=_MAX_JSON):
        self._require_token()
        parsed = urlsplit(url)
        if parsed.hostname == "api.replicate.com":
            if not url.startswith(_API + "/") or parsed.query or parsed.fragment or parsed.port not in (None, 443):
                raise ProviderError("Unsupported API URL")
        else:
            _https_url(url, delivery=True)
        attempts = 1 + (self.get_retries if method == "GET" else 0)
        for attempt in range(attempts):
            deadline = time.monotonic() + self.timeout
            request = Request(url, data=_json(payload).encode() if payload is not None else None,
                              method=method, headers={"Authorization": f"Bearer {self.token}",
                              "Content-Type": "application/json", "User-Agent": "H1TCH-Content-Studio/1"})
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    if response.geturl() != url:
                        raise ProviderError("Provider redirects are disabled")
                    chunks, received = [], 0
                    read = getattr(response, "read1", response.read)
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError
                        chunk = read(min(65536, maximum + 1 - received))
                        if not chunk:
                            return b"".join(chunks), response.headers
                        received += len(chunk)
                        if received > maximum:
                            raise ProviderError("Provider response exceeds its size limit")
                        chunks.append(chunk)
            except HTTPError as exc:
                status = exc.code
                exc.close()
                retry = status == 429 or 500 <= status < 600
            except (URLError, TimeoutError, OSError):
                status, retry = None, True
            if not retry or attempt == attempts - 1:
                raise _HTTPFailure(status) from None
            time.sleep(min(2 ** attempt, 4))

    def _api(self, method, path, payload=None):
        body, _ = self._request(method, _API + path, payload=payload)
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise ProviderError("Provider returned invalid JSON") from None

    def submit(self, plan, approved_digest, budget_usd):
        if not isinstance(plan, dict):
            raise ProviderError("Expected a generation plan")
        plan = json.loads(_json(plan))
        candidate = dict(plan)
        digest = candidate.pop("digest", None)
        expected_keys = {"plan_id", "created_at", "provider", "model_id", "role", "inputs", "input_schema",
                         "max_cost_usd", "cost_basis", "model_input_hash"}
        if set(candidate) != expected_keys or len(_json(plan).encode()) > 250000:
            raise ProviderError("Invalid generation plan fields")
        if not digest or digest != approved_digest or _hash(candidate) != digest:
            raise ProviderError("Approval must match the exact unchanged plan digest")
        if candidate.get("provider") != "replicate" or not _JOB.fullmatch(str(candidate.get("plan_id", ""))):
            raise ProviderError("Invalid generation plan")
        # Revalidate even if a plan arrived through JSON, rather than our planner.
        checked = plan_generation(candidate.get("model_id"), candidate.get("inputs"), candidate.get("max_cost_usd"),
            role=candidate.get("role"), model_spec={"id": candidate.get("model_id"), "role": candidate.get("role"),
            "input_schema": candidate.get("input_schema")})
        if checked["model_input_hash"] != candidate.get("model_input_hash"):
            raise ProviderError("Plan model/input hash is invalid")
        if self.token and self.token in _json(plan):
            raise ProviderError("Plan contains a credential")
        self._require_token()
        budget = _money(budget_usd)
        with self._state() as state:
            job_id = plan["plan_id"]
            if job_id in state["jobs"]:
                existing = state["jobs"][job_id]
                if existing["plan"]["digest"] != digest:
                    raise ProviderError("Plan ID already belongs to a different plan")
                return json.loads(_json(existing))
            limit = state["budget_limit_usd"]
            if limit is not None and Decimal(budget) != Decimal(limit):
                raise ProviderError("Budget differs from the persistent cap; explicitly change the cap first")
            total = sum((Decimal(j["reserved_max_usd"]) for j in state["jobs"].values()), Decimal(0))
            if total + Decimal(plan["max_cost_usd"]) > Decimal(budget):
                raise ProviderError("Generation would exceed the persistent reservation budget")
            state["budget_limit_usd"] = budget
            state["jobs"][job_id] = {"job_id": job_id, "plan": json.loads(_json(plan)), "created_at": _now(),
                "updated_at": _now(), "status": "submitting", "prediction_id": None, "files": [], "output_urls": [],
                "reserved_max_usd": plan["max_cost_usd"], "actual_cost_usd": None, "error": None}
        model_id = plan["model_id"]
        if ":" in model_id:
            path, payload = "/predictions", {"version": model_id, "input": plan["inputs"]}
        else:
            path, payload = f"/models/{model_id}/predictions", {"input": plan["inputs"]}
        try:
            prediction = self._api("POST", path, payload)
            prediction_id = prediction.get("id")
            if not isinstance(prediction_id, str) or not _ID.fullmatch(prediction_id):
                raise ProviderError("Provider did not return a usable prediction ID")
            # Commit the remote ID before processing status/output fields.
            with self._state() as state:
                job = self._job(state, job_id)
                job["prediction_id"], job["status"] = prediction_id, "starting"
            self._record_prediction(job_id, prediction)
        except (ProviderError, OSError):
            with self._state() as state:
                job = self._job(state, job_id)
                if job["prediction_id"] is None:
                    job["status"] = "unknown"
                    job["error"] = "Submission outcome unknown. Do not resubmit this plan. Reconcile a matching prediction from the provider dashboard. Reservation retained."
                else:
                    job["error"] = "Prediction ID saved; response processing failed. Poll the existing job."
                job["updated_at"] = _now()
        return self.get_job(job_id)

    def _record_prediction(self, job_id, prediction):
        urls = []

        def collect(value, depth=0):
            if depth > 8:
                raise ProviderError("Provider output is nested too deeply")
            if isinstance(value, str):
                if value.startswith("https://"):
                    urls.append(_https_url(value, delivery=True))
            elif isinstance(value, (list, dict)):
                for child in (value.values() if isinstance(value, dict) else value):
                    collect(child, depth + 1)
            if len(urls) > 8:
                raise ProviderError("Too many provider output files")

        collect(prediction.get("output"))
        status = prediction.get("status")
        if status not in _STATUS:
            raise ProviderError("Unknown provider prediction status")
        with self._state() as state:
            job = self._job(state, job_id)
            if prediction.get("id") != job["prediction_id"]:
                raise ProviderError("Provider prediction ID mismatch")
            if job["status"] not in _TERMINAL or status in _TERMINAL:
                job["status"] = status
            job["output_urls"] = list(dict.fromkeys(urls))
            job["updated_at"] = _now()
            job["error"] = "Provider reported generation failure; check the provider dashboard." if status == "failed" else None
            version = prediction.get("version")
            if isinstance(version, str) and re.fullmatch(r"[0-9a-f]{64}", version):
                job["resolved_version"] = version
            job["data_removed"] = prediction.get("data_removed") is True

    def poll(self, job_id):
        job = self.get_job(job_id)
        if not job["prediction_id"]:
            raise ProviderError("Job has no prediction ID. Unknown/submitting jobs require reconciliation, never automatic resubmission")
        if job["files"]:
            return job
        prediction = self._api("GET", f"/predictions/{job['prediction_id']}")
        self._record_prediction(job_id, prediction)
        return self.get_job(job_id)

    def wait(self, job_id, *, timeout=120, interval=3):
        if not 0 <= timeout <= 600 or not 1 <= interval <= 30:
            raise ProviderError("Invalid polling bounds")
        deadline = time.monotonic() + timeout
        job = self.get_job(job_id)
        while job["status"] not in _TERMINAL and time.monotonic() < deadline:
            job = self.poll(job_id)
            if job["status"] not in _TERMINAL:
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        return job

    def reconcile(self, job_id, prediction_id):
        """Bind a founder-selected existing prediction, with exact input/model checks.

        Never creates a prediction, releases a reservation or assumes an invoice.
        A provider prediction whose input expired cannot be verified this way.
        """
        if not isinstance(prediction_id, str) or not _ID.fullmatch(prediction_id):
            raise ProviderError("Invalid prediction ID")
        job = self.get_job(job_id)
        if job["prediction_id"] not in (None, prediction_id):
            raise ProviderError("Job is already bound to another prediction")
        prediction = self._api("GET", f"/predictions/{prediction_id}")
        model, _, version = job["plan"]["model_id"].partition(":")
        if (prediction.get("id") != prediction_id or prediction.get("model") != model
                or prediction.get("input") != job["plan"]["inputs"]
                or (version and prediction.get("version") != version)):
            raise ProviderError("Prediction does not match this plan's exact model and inputs")
        with self._state() as state:
            if any(j["prediction_id"] == prediction_id and j["job_id"] != job_id for j in state["jobs"].values()):
                raise ProviderError("Prediction is already bound to another job")
            self._job(state, job_id)["prediction_id"] = prediction_id
        self._record_prediction(job_id, prediction)
        return self.get_job(job_id)

    def fetch_result(self, job_id):
        job = self.get_job(job_id)
        if job["files"]:
            return job
        if job["status"] != "succeeded":
            raise ProviderError("Only successful jobs have downloadable results; poll first")
        if not job["output_urls"]:
            raise ProviderError("No downloadable output remains. Replicate output normally expires after one hour")
        directory = self.workspace / job_id
        if directory.is_symlink():
            raise ProviderError("Job output directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        files = []
        for index, url in enumerate(job["output_urls"]):
            _https_url(url, delivery=True)
            body, _ = self._request("GET", url, maximum=self.max_download_bytes)
            media, extension = self._media_type(body, job["plan"]["role"])
            path = directory / f"output-{index:02d}{extension}"
            # Filename comes from us, never URL paths or Content-Disposition.
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".download-", delete=False) as out:
                temporary = out.name
                try:
                    os.chmod(temporary, 0o600)
                    out.write(body)
                    out.flush()
                    os.fsync(out.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            files.append({"path": str(path), "sha256": hashlib.sha256(body).hexdigest(),
                          "bytes": len(body), "media_type": media})
        with self._state() as state:
            job = self._job(state, job_id)
            job["files"], job["updated_at"] = files, _now()
        return self.get_job(job_id)

    @staticmethod
    def _media_type(body, role):
        if role in {"broll", "presenter"}:
            if len(body) >= 12 and body[4:8] == b"ftyp":
                return "video/mp4", ".mp4"
            if body.startswith(b"\x1aE\xdf\xa3"):
                return "video/webm", ".webm"
        if role == "image":
            if body.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png", ".png"
            if body.startswith(b"\xff\xd8\xff"):
                return "image/jpeg", ".jpg"
            if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
                return "image/webp", ".webp"
        if role == "voice":
            if body.startswith(b"RIFF") and body[8:12] == b"WAVE":
                return "audio/wav", ".wav"
            if body.startswith(b"ID3") or (len(body) > 1 and body[0] == 255 and body[1] & 224 == 224):
                return "audio/mpeg", ".mp3"
            if body.startswith(b"fLaC"):
                return "audio/flac", ".flac"
        raise ProviderError("Downloaded bytes are not a supported media type for this model role")
