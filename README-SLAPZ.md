# Slapz Studio

This fork adds a local content editor to [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo). It builds Slapz photo posts, swipe carousels, slideshow videos and demo/presenter Reels from ordered media and editable text. Upstream's generator remains available; the Slapz page has its own predictable composition pipeline.

## Run the editor

Requires Python 3.11+ (3.12 tested), [uv](https://docs.astral.sh/uv/), and `ffmpeg`/`ffprobe` on PATH. On macOS, `brew install ffmpeg`; on Ubuntu, install `ffmpeg fonts-dejavu-core`. The renderer uses system Arial or DejaVu Sans. Set `STUDIO_FONT` and `STUDIO_FONT_BOLD` to local font files to choose your own fonts; font hashes are recorded with each render.

```sh
uv sync --frozen --python 3.12 --only-group studio
uv run --no-sync streamlit run webui/pages/1_Slapz_Studio.py --server.address 127.0.0.1 --server.port 8502
```

Open http://127.0.0.1:8502. The dedicated page works without the upstream cloud-speech dependencies or an API key. To use the full upstream app, follow [its setup](README-en.md); **Slapz Studio** also appears in its Streamlit pages menu.

1. Choose single image, swipe carousel, slideshow video or demo/presenter Reel, and a 9:16, 4:5 or square canvas.
2. Upload your photos/recordings, select them in each scene and arrange their order. Add a hook or longer paragraph, choose its layout and placement, and set video timing.
3. For video, select narration/music and retain original clip audio where needed. Scene cuts keep their source timing; source ranges must exist. Images preserve their framing, with no automatic zoom or crop. Music loops and ducks under voice.
4. Render, review the preview, and download the draft. Single images and carousels export JPEGs; carousels also include an ordered ZIP. Videos export H.264/AAC MP4 at 30 fps. Every render includes hashes and a manifest.
5. Use **Recipes** to save the project with exact source-media copies in a private local library. Reopen it after a browser/server restart, edit text or rebind scene media, and save a new recipe version. You can also save a project JSON to continue later. UI projects use uploaded asset references; re-upload the same original files before importing a saved project. CLI projects instead use local file paths relative to their JSON file.

The project allows up to 20 scenes and 180 seconds of video. The renderer refuses overflowing text or invalid media before export. It requires a new/empty output directory to preserve earlier drafts. It adds no automatic product watermark; marks and provenance already in source/generated media can remain.

This is a local single-operator tool. Keep it bound to loopback; it is not an authenticated hosted service. Drafts, uploads and generation state live under the ignored `storage/studio/` directory. Back up that directory and saved projects if you want to retain them.

## CLI and offline examples

```sh
uv run --no-sync python -m app.studio render examples/studio/carousel.json --output storage/studio/my-carousel
uv run --no-sync python -m app.studio render examples/studio/slideshow.json --output storage/studio/my-slideshow
uv run --no-sync python -m app.studio new --format reel --size portrait --output storage/studio/my-reel.json
uv run --no-sync python -m app.studio validate storage/studio/my-reel.json
uv run --no-sync python -m app.studio schema
```

For an actual recording, use a scene such as:

```json
{"source":"capture.mp4","source_kind":"video","source_start":0,"duration":5,"preserve_audio":true,"headline":"your words. your voice.","layout":"top","text_position":"top"}
```

Set `preserve_audio` to false for silent screen recordings. Top-level `narration_file`, `music_file` and `music_volume` control added audio. Source clips are never looped to invent missing footage; shorten a scene if it exceeds the source. Narration must fit the timeline.

Video projects also accept timed `captions`: `[{"start":0.5,"end":2.5,"text":"Your subtitle"}]`, measured in seconds across the final timeline. Enter/edit cues or upload an SRT in the editor, or pass `render --captions-srt captions.srt` in the CLI. Cues must be ordered, non-overlapping and within the video; up to 100 cues are supported. These are burned into the video separately from persistent scene text. Automatic speech transcription and word-by-word animated captions are not implemented in this page.

## Reusable Slapz recipes

The **Recipes** tab has three starting structures: **Headphone reaction** (image or video plus a real beat), **Three-beat choice** (three audio excerpts or existing video clips with their sound), and **Real app creation demo** (idea, creation, result recordings). Upload authorized media, bind every slot and choose **Use this recipe**. It opens an editable project in **Create**; check scene lengths and source ranges against your actual clips before rendering.

For **Three-beat choice**, choose **Audio excerpts**, upload/select three MP3, WAV, M4A, AAC, OGG or FLAC files, and set each start and duration (0.5–15 seconds, default 4). **Prepare audio excerpts and use recipe** validates all ranges, creates waveform MP4s locally and opens three editable scenes. The waveform stays below the short top hook and mood label. Original audio plays without normalization, replacement or looping; the clip is AAC-encoded, while the original file bytes remain saved separately. Durations round to 30fps frames. To change excerpts after reopening a recipe, select the retained originals in Recipes and prepare again. No provider upload or paid generation occurs.

**Save current project as recipe** retains text, scene order, timing, source ranges, audio, captions and generated-media flags, plus exact copies of the used media. Prepared waveform clips retain their original audio dependency and excerpt settings; reopening restores both files without rerendering. Portable project JSON also records these dependencies, so import requires re-uploading the prepared clips and their original audio. **Load saved recipe** restores that media into a new editor session without re-uploading. Every save creates a new version. The private library is ignored `storage/studio/recipes/`; JSON uses content-addressed asset references, and loading verifies hashes and rejects paths outside the library. Back up that entire folder, not only its JSON files. This is local storage, not a provider upload or a generation request.

For local production integrations, `app.studio.recipes.save_recipe(name, project, assets, library_root, source_root=studio_storage)` accepts a validated project and the asset map returned by `ui_helpers.store_upload`. Source assets must already be under the trusted Studio storage root. `load_recipe(recipe_id, library_root, session_uploads)` returns `(project, assets)` ready for the editor. `bind_template(template_id, slot_asset_ids, assets)` creates one of the three built-in structures. Private media and project paths do not belong in committed examples.

## Replicate assets

The **Replicate assets** tab prepares a model-specific plan, shows its inputs and spending reservation, and requires a separate approval before submission. Nothing is generated by opening the page or rendering locally. Set `REPLICATE_API_TOKEN` in the server's environment through your own credential manager; never put credentials into a project, prompt, model input, screenshot or commit. The adapter does not read HQ's environment file.

The dated catalog in [`app/studio/catalog.json`](app/studio/catalog.json) includes Veo 3.1 for supporting video, HeyGen Avatar IV for presenters, MiniMax Speech-02-HD for narration, FLUX.1 schnell for images, and the preferred Nano Banana Pro casting model. The Pro entry is a deliberately narrow reviewed subset (1K, 9:16 JPEG, at most one HTTPS reference), requires `allow_fallback_model: false`, and leaves provider safety settings unchanged. These have different input contracts. HeyGen needs valid avatar/voice IDs; its mapped API does not accept arbitrary photo/audio uploads. Real app screens should come from recordings. Each entry links to its provider schema and records watermark/provenance limits. Veo's documentation specifies SynthID; there is no blanket promise that every provider output is watermark-free.

```sh
# Planning is local and free; inputs.json contains the selected model's actual input fields.
uv run --no-sync python -m app.studio catalog
uv run --no-sync python -m app.studio plan-generation --model black-forest-labs/flux-schnell --inputs inputs.json --max-cost-usd 1 --output storage/studio/generation.json

# Paid: only after reviewing the plan and current provider pricing.
uv run --no-sync python -m app.studio generate storage/studio/generation.json --approve-digest REVIEWED_DIGEST --budget-usd 10
uv run --no-sync python -m app.studio jobs
uv run --no-sync python -m app.studio poll JOB_ID
uv run --no-sync python -m app.studio fetch JOB_ID
```

The dollar values are **caller-declared reservations**, not a provider quote or enforced invoice ceiling. Reservations are saved before submission and remain counted even for failed or uncertain requests. The adapter never retries a prediction POST automatically; the same plan is idempotent. Status refresh and downloads use the saved prediction ID. Save successful outputs promptly because provider URLs expire. Retrieved files can be added to the editor's media library.

`budget` prints the ledger; `budget --set-limit-usd AMOUNT --approve-change` deliberately changes its cap. For an uncertain submission, first identify its prediction in Replicate, then use `reconcile JOB_ID --prediction-id PREDICTION_ID`. Reconciliation only reads and requires an exact model/input match. Preparing a new plan is a new generation, even if its prompt is identical.

Custom owner/name models and owner/name:64-hex-version community models work through `plan-generation --model-spec model.json`. The spec needs the matching `id`, a `role` (`image`, `broll`, `presenter`, `voice`), and an explicit `input_schema`; use the catalog as a template and check the actual model documentation. The supported schema subset is enforced locally, without external references or executable hooks. The UI picker uses catalog entries. Media inputs must be public HTTPS URLs without credentials/query strings; local/data URLs and automatic asset uploading are not implemented. The adapter accepts standard image/video/audio results hosted on `replicate.delivery`, not arbitrary result websites or deployment endpoints.

## HQ distribution and measurement

The preview's **HQ handoff** export packages the ordered assets, caption, account, AI disclosure, content hashes, creative/campaign IDs and tracked destination as an unapproved local draft. It verifies that rendered bytes still match the manifest. CLI equivalent:

```sh
uv run --no-sync python -m app.studio handoff storage/studio/my-carousel --account @getslapz --caption "Your reviewed caption" --campaign slapz-content --destination https://slapz.ai/ --output storage/studio/my-carousel/handoff.json
```

For a supplied AppsFlyer OneLink, the export adds the AppsFlyer campaign/channel/creative fields as well as UTMs. It does not create OneLinks, configure SDKs or prove attribution. A reused bio link cannot distinguish individual post clicks.

Zernio publishing, Resend email and the AppsFlyer/PostHog/RevenueCat reporting loop remain HQ integrations. This fork produces the reviewable input for them; it does not call them or automatically schedule, post, email or spend. HQ must validate its platform plan, upload the approved files, bind approval to the exact draft and apply its existing per-action approval flow. The exported handoff is not itself a validated HQ publishing plan.

## Verification and limits

```sh
uv sync --frozen --python 3.12 --only-group studio --only-group dev
uv run --no-sync ruff check app/studio test/studio webui/pages/1_Slapz_Studio.py
uv run --no-sync python -m pytest -q test/studio
```

Tests exercise actual local media rendering and mocked Replicate failure/recovery paths. Paid generation, model quality, current invoice cost and end-to-end publishing are not established by those tests. The original MIT license and upstream notices remain intact; model and media terms are separate. This fork is an implemented local pilot, not evidence yet that Heycatch can be cancelled.
