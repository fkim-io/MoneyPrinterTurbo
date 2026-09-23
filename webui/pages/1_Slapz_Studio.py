"""Slapz's local draft editor, available through MPT's Streamlit pages menu."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.studio import StudioProject, parse_srt, render_project  # noqa: E402
from app.studio.ui_helpers import (  # noqa: E402
    export_project,
    import_project,
    local_output_path,
    make_download_bundle,
    store_upload,
)


st.set_page_config(page_title="Slapz Studio", page_icon="✦", layout="wide")
st.markdown(
    """<style>
    .stApp {background:#111114; color:#f5f1f5}
    [data-testid="stHeader"] {background:#111114}
    .block-container {max-width:1440px; padding-top:2.5rem}
    [data-testid="stVerticalBlockBorderWrapper"] {border-color:#343038}
    [data-testid="stCaptionContainer"] {color:#bcb3c1}
    h1, h2, h3 {letter-spacing:-.025em}
    .studio-label {color:#ff60b5; font-size:.8rem; font-weight:700;
                   letter-spacing:.15em; margin-bottom:.65rem}
    .studio-deck {color:#bcb3c1; max-width:48rem; margin-bottom:1.5rem}
    </style>""",
    unsafe_allow_html=True,
)

FORMATS = {
    "image": "Single image",
    "carousel": "Swipe carousel",
    "slideshow": "Slideshow video",
    "reel": "Demo / presenter Reel",
}
SIZES = {"portrait": "9:16 · 1080 × 1920", "feed": "4:5 · 1080 × 1350", "square": "1:1 · 1080 × 1080"}
LAYOUTS = {"boxed": "Headline boxes", "paragraph": "Paragraph overlay", "top": "Top hook", "minimal": "Minimal caption"}


def new_scene(**overrides):
    return {
        "_id": uuid4().hex,
        "source": None,
        "source_kind": "color",
        "headline": "",
        "body": "",
        "layout": "boxed",
        "duration": 4.0,
        "source_start": 0.0,
        "preserve_audio": False,
        "text_position": "center",
        **overrides,
    }


if "slapz_editor" not in st.session_state:
    st.session_state.slapz_editor = {
        "title": "Slapz creative",
        "creative_id": "slapz-draft",
        "format": "carousel",
        "size": "portrait",
        "scenes": [
            new_scene(headline="Your hook goes here", body="Start with one clear idea."),
            new_scene(headline="Show the moment", body="Add your own photo or app recording."),
            new_scene(headline="Make it a Slapz", body="Write the next step in your own voice."),
        ],
        "narration_file": None,
        "music_file": None,
        "music_volume": 0.15,
    }
    st.session_state.slapz_assets = {}
    st.session_state.slapz_session = uuid4().hex
    st.session_state.slapz_revision = 0

WORKSPACE = ROOT / "storage" / "studio" / "sessions" / st.session_state.slapz_session
ASSETS = st.session_state.slapz_assets
PROJECT = st.session_state.slapz_editor
PROJECT.setdefault("captions", [])
REVISION = st.session_state.slapz_revision


def snapshot():
    return {**PROJECT, "scenes": [{k: v for k, v in scene.items() if k != "_id"} for scene in PROJECT["scenes"]]}


def digest(project):
    return hashlib.sha256(json.dumps(project, sort_keys=True).encode()).hexdigest()


def add_upload(upload):
    asset = store_upload(upload.getvalue(), upload.name, WORKSPACE / "uploads")
    asset["generated"] = bool(ASSETS.get(asset["id"], {}).get("generated"))
    ASSETS[asset["id"]] = asset
    return asset


def show_error(error):
    if hasattr(error, "errors"):
        details = error.errors(include_input=False)
        for detail in details[:6]:
            label = " → ".join(str(part) for part in detail.get("loc", []))
            st.error(f"{label}: {detail['msg']}" if label else detail["msg"])
    else:
        st.error(str(error))


def edit_scene(scene, index):
    prefix = f"scene_{REVISION}_{scene['_id']}"
    with st.expander(f"{index + 1:02d} · {scene['headline'][:48] or 'Untitled scene'}", expanded=index == 0):
        controls = st.columns([5, 1, 1, 1])
        controls[0].caption("Slide order is the export order.")
        if controls[1].button("↑", key=prefix + "_up", disabled=index == 0, help="Move earlier"):
            PROJECT["scenes"][index - 1], PROJECT["scenes"][index] = scene, PROJECT["scenes"][index - 1]
            st.rerun()
        if controls[2].button("↓", key=prefix + "_down", disabled=index == len(PROJECT["scenes"]) - 1, help="Move later"):
            PROJECT["scenes"][index + 1], PROJECT["scenes"][index] = scene, PROJECT["scenes"][index + 1]
            st.rerun()
        if controls[3].button("×", key=prefix + "_remove", disabled=len(PROJECT["scenes"]) == 1, help="Remove scene"):
            PROJECT["scenes"].pop(index)
            st.rerun()
        media = {key: value for key, value in ASSETS.items() if value["kind"] in {"image", "video"}}
        current = next((key for key, value in media.items() if value["path"] == scene.get("source")), None)
        options = [None, *media]
        selected = st.selectbox(
            "Background", options, index=options.index(current),
            format_func=lambda key: "Slapz color card" if key is None else media[key]["name"],
            key=prefix + "_source",
        )
        previous_kind = scene["source_kind"]
        scene["source"] = media[selected]["path"] if selected else None
        scene["source_kind"] = media[selected]["kind"] if selected else "color"
        if scene["source_kind"] == "video" and previous_kind != "video":
            scene["preserve_audio"] = True
            st.session_state[prefix + "_audio"] = True
        elif scene["source_kind"] != "video":
            scene["source_start"] = 0.0
            scene["preserve_audio"] = False
        scene["headline"] = st.text_area("Headline / hook", scene["headline"], key=prefix + "_headline", height=85)
        scene["body"] = st.text_area("Supporting text", scene["body"], key=prefix + "_body", height=110)
        columns = st.columns(2)
        scene["layout"] = columns[0].selectbox(
            "Text layout", list(LAYOUTS), index=list(LAYOUTS).index(scene["layout"]),
            format_func=LAYOUTS.get, key=prefix + "_layout",
        )
        scene["text_position"] = columns[1].selectbox(
            "Text placement", ["top", "center", "bottom"],
            index=["top", "center", "bottom"].index(scene["text_position"]), key=prefix + "_position",
        )
        if PROJECT["format"] in {"slideshow", "reel"}:
            scene["duration"] = st.number_input(
                "Scene length (seconds)", min_value=0.2, max_value=60.0,
                value=float(scene["duration"]), step=0.5, key=prefix + "_duration",
            )
        if scene["source_kind"] == "video":
            scene["source_start"] = st.number_input(
                "Start in source clip (seconds)", min_value=0.0,
                value=float(scene["source_start"]), step=0.5, key=prefix + "_start",
            )
            scene["preserve_audio"] = st.checkbox(
                "Keep this clip’s original audio", value=scene["preserve_audio"], key=prefix + "_audio",
                help="Keep presenter dialogue and app audio in sync. Added voiceover and music will mix with it.",
            )
            if PROJECT["format"] in {"image", "carousel"}:
                st.warning("Choose a photo for this scene, or switch to a video format.")


def audio_controls():
    with st.expander("Sound · voiceover and music"):
        uploads = st.file_uploader(
            "Upload audio", type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
            accept_multiple_files=True, key=f"audio_upload_{REVISION}",
        )
        for upload in uploads:
            try:
                add_upload(upload)
            except ValueError as error:
                show_error(error)
        audio = {key: asset for key, asset in ASSETS.items() if asset["kind"] == "audio"}
        options = [None, *audio]
        for key, label in (("narration_file", "Voiceover"), ("music_file", "Background music")):
            current = next((asset_id for asset_id, asset in audio.items() if asset["path"] == PROJECT.get(key)), None)
            selected = st.selectbox(
                label, options, index=options.index(current),
                format_func=lambda asset_id: "None" if asset_id is None else audio[asset_id]["name"],
                key=f"{key}_{REVISION}",
            )
            PROJECT[key] = audio[selected]["path"] if selected else None
        PROJECT["music_volume"] = st.slider("Music volume", 0.0, 1.0, float(PROJECT["music_volume"]), 0.05, key=f"volume_{REVISION}")
        st.caption("Use music and footage you have permission to include. Presenter dialogue stays on its scene’s audio track.")


def caption_controls():
    with st.expander("Timed captions · manual or SRT"):
        st.caption("Times are seconds from the start of the complete video. Captions appear in the lower safe area; scene text stays above them.")
        srt_file = st.file_uploader("Upload SRT captions", type=["srt"], key=f"srt_file_{REVISION}")
        srt_text = st.text_area("Or paste SRT text", key=f"srt_text_{REVISION}", height=100)
        if st.button("Replace captions from SRT", disabled=srt_file is None and not srt_text.strip()):
            try:
                if srt_file is not None and srt_file.size > 1024 * 1024:
                    raise ValueError("SRT files must be smaller than 1 MB.")
                text = srt_file.getvalue().decode("utf-8-sig") if srt_file is not None else srt_text
                captions = parse_srt(text)
                StudioProject.model_validate({**snapshot(), "captions": captions})
                PROJECT["captions"] = captions
                st.session_state.slapz_revision += 1
                st.rerun()
            except (ValueError, UnicodeDecodeError) as error:
                show_error(error)
        for index, cue in enumerate(PROJECT["captions"]):
            key = f"caption_{REVISION}_{index}"
            with st.container(border=True):
                columns = st.columns([1, 1, 1])
                cue["start"] = columns[0].number_input("Start (s)", min_value=0.0, value=float(cue["start"]), step=0.1, key=key + "_start")
                cue["end"] = columns[1].number_input("End (s)", min_value=0.0, value=float(cue["end"]), step=0.1, key=key + "_end")
                if columns[2].button("Remove caption", key=key + "_remove"):
                    PROJECT["captions"].pop(index)
                    st.session_state.slapz_revision += 1
                    st.rerun()
                cue["text"] = st.text_area("Caption text", cue["text"], key=key + "_text", height=85)
        timeline = sum(scene["duration"] for scene in PROJECT["scenes"])
        start = PROJECT["captions"][-1]["end"] if PROJECT["captions"] else 0.0
        if st.button("Add timed caption", disabled=len(PROJECT["captions"]) >= 100 or start >= timeline):
            PROJECT["captions"].append({"start": start, "end": min(start + 2.0, timeline), "text": "Your caption goes here."})
            st.rerun()
        if PROJECT["captions"] and st.button("Remove all timed captions"):
            PROJECT["captions"] = []
            st.session_state.slapz_revision += 1
            st.rerun()


def show_preview():
    result = st.session_state.get("slapz_result")
    with st.container(border=True):
        st.subheader("Draft preview")
        if result is None:
            st.markdown("Your next post starts here.")
            st.caption("Choose a format, add your text and media, then render a draft. Placeholder cards are ready to try.")
            return
        if result["digest"] != digest(snapshot()):
            st.warning("This preview is from an earlier version. Render again to include your latest edits.")
        manifest = result["manifest"]
        output_dir = Path(result["output_dir"])
        st.caption(f"DRAFT · {result['title']} · {len(manifest.get('assets', []))} output files")
        for index, asset in enumerate(manifest.get("assets", [])):
            path = local_output_path(asset["path"], output_dir)
            if asset["type"] == "image":
                st.image(str(path), caption=f"Slide {index + 1}", width="stretch")
            elif asset["type"] == "video":
                st.video(str(path))
            st.download_button(
                f"Download {path.name}", path.read_bytes(), path.name,
                mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                key=f"asset_download_{result['digest']}_{index}", width="stretch",
            )
        st.download_button(
            "Download complete draft ZIP", result["zip"], f"{result['creative_id']}-draft.zip",
            mime="application/zip", type="primary", width="stretch",
        )
        st.caption("Exports remain drafts for review. Rendering does not schedule or publish a post.")
        with st.expander("HQ handoff · caption and tracking"):
            from app.studio.handoff import build_handoff

            suffix = result["digest"]
            caption = st.text_area("Post caption", key=f"handoff_caption_{suffix}")
            account = st.text_input("Account", "@getslapz", key=f"handoff_account_{suffix}")
            campaign = st.text_input("Campaign ID", "slapz-content", key=f"handoff_campaign_{suffix}")
            landing_url = st.text_input("Landing URL", "https://slapz.ai/", key=f"handoff_landing_{suffix}")
            generated_paths = {asset["path"] for asset in ASSETS.values() if asset.get("generated")}
            project_paths = {scene.get("source") for scene in result["project"]["scenes"]}
            project_paths.update(result["project"].get(key) for key in ("narration_file", "music_file"))
            ai_generated = st.checkbox(
                "Includes AI-generated media", value=bool(project_paths & generated_paths), key=f"handoff_ai_{suffix}",
            )
            handoff_inputs = {"platform": "instagram", "caption": caption, "account": account, "campaign_id": campaign, "landing_url": landing_url, "ai_generated": ai_generated}
            handoff_digest = digest({"render": suffix, **handoff_inputs})
            if st.button("Prepare HQ handoff", key=f"handoff_prepare_{suffix}"):
                try:
                    handoff = build_handoff(output_dir, **handoff_inputs)
                    st.session_state.slapz_handoff = {"digest": handoff_digest, "data": json.dumps(handoff, indent=2, ensure_ascii=False).encode()}
                except (ValueError, OSError) as error:
                    show_error(error)
            handoff = st.session_state.get("slapz_handoff")
            if handoff and handoff["digest"] == handoff_digest:
                st.download_button("Download HQ handoff JSON", handoff["data"], f"{result['creative_id']}-handoff.json", "application/json")
            st.caption("Creates a local review package for HQ. Scheduling, social posting and email remain separate approval steps.")


def create_tab():
    editor, preview = st.columns([1.25, 1], gap="large")
    with editor:
        st.subheader("01 · Choose your format")
        PROJECT["format"] = st.radio(
            "Post format", list(FORMATS), index=list(FORMATS).index(PROJECT["format"]),
            format_func=FORMATS.get, horizontal=True, key=f"format_{REVISION}", label_visibility="collapsed",
        )
        PROJECT["size"] = st.selectbox(
            "Canvas", list(SIZES), index=list(SIZES).index(PROJECT["size"]), format_func=SIZES.get, key=f"size_{REVISION}",
        )
        title_col, id_col = st.columns([1.5, 1])
        PROJECT["title"] = title_col.text_input("Project title", PROJECT["title"], key=f"title_{REVISION}")
        PROJECT["creative_id"] = id_col.text_input(
            "Creative ID", PROJECT["creative_id"], help="Letters, numbers, hyphens and underscores; used in export filenames.", key=f"creative_{REVISION}",
        )
        st.subheader("02 · Build your scenes")
        with st.expander("Media library · upload photos and clips", expanded=True):
            uploads = st.file_uploader(
                "Your media", type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v"],
                accept_multiple_files=True, key=f"media_upload_{REVISION}",
            )
            current_uploads = []
            for upload in uploads:
                try:
                    current_uploads.append(add_upload(upload))
                except ValueError as error:
                    show_error(error)
            st.caption("Pick an upload in each scene’s Background menu. Files stay in this local workspace.")
            if current_uploads and st.button("Append uploads as scenes", disabled=len(PROJECT["scenes"]) + len(current_uploads) > 20):
                for asset in current_uploads:
                    PROJECT["scenes"].append(new_scene(source=asset["path"], source_kind=asset["kind"], preserve_audio=asset["kind"] == "video"))
                st.rerun()
        if PROJECT["format"] == "image" and len(PROJECT["scenes"]) != 1:
            st.info("A single image uses one scene. Keep the first scene or choose a carousel/video.")
            if st.button("Keep only the first scene"):
                PROJECT["scenes"] = PROJECT["scenes"][:1]
                st.rerun()
        for index, scene in enumerate(PROJECT["scenes"]):
            edit_scene(scene, index)
        if st.button("＋ Add color card", disabled=len(PROJECT["scenes"]) >= 20 or PROJECT["format"] == "image"):
            PROJECT["scenes"].append(new_scene())
            st.rerun()
        if PROJECT["format"] in {"slideshow", "reel"}:
            st.caption(f"{len(PROJECT['scenes'])} scenes · {sum(scene['duration'] for scene in PROJECT['scenes']):g} seconds · static images keep their framing")
            audio_controls()
            caption_controls()
        elif PROJECT.get("music_file") or PROJECT.get("narration_file"):
            st.warning("Still-image exports do not use audio. Remove it or choose a video format.")
            if st.button("Remove audio from this project"):
                PROJECT["music_file"] = PROJECT["narration_file"] = None
                st.session_state.slapz_revision += 1
                st.rerun()
        if PROJECT["format"] in {"image", "carousel"} and PROJECT["captions"]:
            st.warning("Timed captions belong to video formats. Remove them or choose a video format.")
            if st.button("Remove timed captions from this project"):
                PROJECT["captions"] = []
                st.session_state.slapz_revision += 1
                st.rerun()
        st.subheader("03 · Review your draft")
        st.caption("Long text is allowed. The renderer checks that it fits before exporting.")
        if st.button("Render draft", type="primary", width="stretch"):
            try:
                project = StudioProject.model_validate(snapshot())
                output_dir = WORKSPACE / "renders" / uuid4().hex
                with st.spinner("Composing your draft…"):
                    manifest = render_project(project, output_dir)
                    bundle = make_download_bundle(manifest, output_dir)
                st.session_state.slapz_result = {
                    "manifest": manifest, "output_dir": str(output_dir), "zip": bundle,
                    "digest": digest(snapshot()), "title": PROJECT["title"], "creative_id": PROJECT["creative_id"],
                    "project": snapshot(),
                }
            except (ValueError, OSError, RuntimeError) as error:
                show_error(error)
    with preview:
        try:
            show_preview()
        except (ValueError, OSError) as error:
            show_error(error)


def project_tab():
    st.subheader("Keep your project editable")
    st.write("Save the scene order, text, timing and audio choices. The JSON uses asset references; keep your original media alongside it.")
    try:
        st.download_button(
            "Download project JSON", export_project(snapshot(), ASSETS),
            f"{PROJECT['creative_id'] or 'slapz-project'}.json", "application/json",
        )
    except ValueError as error:
        show_error(error)
    uploaded = st.file_uploader("Import a saved Slapz Studio project", type=["json"], key="slapz_project_import")
    st.caption("Upload its original photos, clips and audio into the media library first. Import replaces the current editor.")
    if st.button("Import project", disabled=uploaded is None):
        try:
            bound = import_project(uploaded.getvalue(), ASSETS)
            validated = StudioProject.model_validate(bound).model_dump(mode="json")
            validated["scenes"] = [new_scene(**scene) for scene in validated["scenes"]]
            st.session_state.slapz_editor = validated
            st.session_state.slapz_revision += 1
            st.rerun()
        except (ValueError, OSError) as error:
            show_error(error)


def restore_editor(project):
    validated = StudioProject.model_validate(project).model_dump(mode="json")
    validated["scenes"] = [new_scene(**scene) for scene in validated["scenes"]]
    st.session_state.slapz_editor = validated
    st.session_state.slapz_revision += 1
    st.session_state.pop("slapz_result", None)
    st.rerun()


def recipes_tab():
    from app.studio.recipes import bind_template, list_recipes, load_recipe, save_recipe, templates

    library = ROOT / "storage" / "studio" / "recipes"
    st.subheader("Start from a recipe")
    st.write("Reuse a Slapz structure, or reopen a saved draft with its original media, text and timing.")
    choices = {item["id"]: item for item in templates()}
    selected = st.selectbox("Recipe template", list(choices), format_func=lambda key: choices[key]["name"])
    template = choices[selected]
    st.caption(template["description"])
    uploads = st.file_uploader(
        "Recipe media", type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v", "mp3", "wav", "m4a", "aac", "ogg", "flac"],
        accept_multiple_files=True, key=f"recipe_upload_{REVISION}",
    )
    for upload in uploads:
        try:
            add_upload(upload)
        except ValueError as error:
            show_error(error)
    audio_mode = selected == "music-choice" and st.radio(
        "Beat sources", ["Audio excerpts", "Existing video clips"], horizontal=True, key="recipe_beat_mode",
    ) == "Audio excerpts"
    if audio_mode:
        st.caption("Select three audio files and their exact excerpts. Preparation runs locally with FFmpeg, retaining the original audio and a waveform clip. Text stays above the waveform.")
    bindings = {}
    excerpts = []
    for slot in template["slots"]:
        kinds = ["audio"] if audio_mode else slot["kinds"]
        eligible = {key: asset for key, asset in ASSETS.items() if asset["kind"] in kinds}
        bindings[slot["id"]] = st.selectbox(
            slot["label"].replace(" clip", " audio") if audio_mode else slot["label"], [None, *eligible],
            format_func=lambda key, media=eligible: "Choose uploaded media" if key is None else media[key]["name"],
            key=f"recipe_{selected}_{slot['id']}_{audio_mode}_{REVISION}",
        )
        if audio_mode:
            columns = st.columns(2)
            start = columns[0].number_input(f"{slot['label'].replace(' clip', '')} start (seconds)", min_value=0.0, max_value=86400.0, value=0.0, step=0.5, key=f"recipe_start_{slot['id']}_{REVISION}")
            duration = columns[1].number_input(f"{slot['label'].replace(' clip', '')} duration (seconds)", min_value=0.5, max_value=15.0, value=4.0, step=0.5, key=f"recipe_duration_{slot['id']}_{REVISION}")
            excerpts.append({"asset_id": bindings[slot["id"]], "source_start": start, "duration": duration})
    st.caption("Loading replaces the current editor. Save your current project first. No generation or upload to a provider occurs.")
    action = "Prepare audio excerpts and use recipe" if audio_mode else "Use this recipe"
    if st.button(action, disabled=not all(bindings.values())):
        try:
            if audio_mode:
                from app.studio.audio_recipe import prepare_audio_choice

                with st.spinner("Preparing three waveform clips locally…"):
                    project, prepared = prepare_audio_choice(excerpts, ASSETS, WORKSPACE / "prepared", source_root=ROOT / "storage" / "studio")
                ASSETS.update(prepared)
                restore_editor(project)
            else:
                restore_editor(bind_template(selected, bindings, ASSETS))
        except (ValueError, OSError) as error:
            show_error(error)
    st.divider()
    st.subheader("Your saved recipes")
    st.caption("Private local storage keeps exact copies of the selected source files. Each save creates a new version; back up storage/studio/recipes to retain them.")
    name = st.text_input("Recipe name", PROJECT["title"], key=f"recipe_name_{REVISION}")
    if st.button("Save current project as recipe"):
        try:
            saved = save_recipe(name, snapshot(), ASSETS, library, source_root=ROOT / "storage" / "studio")
            st.success(f"Saved {saved['name']}. It can be reopened after restarting the editor.")
        except (ValueError, OSError) as error:
            show_error(error)
    try:
        saved = {item["id"]: item for item in list_recipes(library)}
        if saved:
            choice = st.selectbox("Saved recipe", list(saved), format_func=lambda key: f"{saved[key]['name']} · {saved[key]['created_at'][:19]}")
            if st.button("Load saved recipe"):
                bound, assets = load_recipe(choice, library, WORKSPACE / "uploads")
                ASSETS.update(assets)
                restore_editor(bound)
        else:
            st.info("Save a project here to keep its media and reopen it without re-uploading.")
    except (ValueError, OSError) as error:
        show_error(error)


def generation_tab():
    from app.studio.providers import ProviderError, ReplicateProvider, load_catalog, plan_generation

    st.subheader("Generate a source asset")
    st.write("Create an image, supporting video, presenter clip or voiceover, then add it to your scene library.")
    catalog = load_catalog()["models"]
    models = {model["id"]: model for model in catalog}
    selected = st.selectbox("Replicate model", list(models), index=list(models).index("google/nano-banana-pro"), format_func=lambda model_id: models[model_id]["name"])
    model = models[selected]
    st.caption(model["description"])
    if model.get("provenance"):
        st.info(str(model["provenance"]))
    if model.get("source_url"):
        st.markdown(f"[Model details and input documentation]({model['source_url']})")
    inputs_text = st.text_area(
        "Model inputs (JSON)", json.dumps(model.get("example_inputs", {}), indent=2), height=220, key=f"replicate_inputs_{selected}",
        help="Models use different input fields. Media URLs supplied here are sent to Replicate only when you explicitly generate.",
    )
    max_cost = st.number_input("Maximum budget reservation for this generation (USD)", min_value=0.01, value=1.0, step=0.25, format="%.2f")
    st.caption("This is your declared spending reservation, not a provider price quote or an invoice ceiling. Check the model’s pricing before generating.")
    if st.button("Prepare generation plan"):
        try:
            inputs = json.loads(inputs_text)
            st.session_state.slapz_generation_plan = plan_generation(selected, inputs, max_cost)
        except (ValueError, TypeError) as error:
            show_error(error)
    try:
        provider = ReplicateProvider(ROOT / "storage" / "studio" / "replicate")
        budget = provider.budget_summary()
    except (ProviderError, OSError) as error:
        show_error(error)
        return
    plan = st.session_state.get("slapz_generation_plan")
    if plan:
        st.divider()
        st.markdown("**Review this generation**")
        st.json({"model": plan["model_id"], "inputs": plan["inputs"], "reserved_usd": plan["max_cost_usd"], "plan_id": plan["plan_id"]})
        current = None
        try:
            current = plan_generation(selected, json.loads(inputs_text), max_cost)
        except (ValueError, TypeError):
            pass
        changed = current is None or current["model_input_hash"] != plan["model_input_hash"] or str(current["max_cost_usd"]) != str(plan["max_cost_usd"])
        if changed:
            st.warning("The model, inputs or reservation changed. Prepare a fresh plan before generating.")
        configured_budget = budget.get("budget_limit_usd")
        total_budget = st.number_input(
            "Workspace generation budget (USD)", min_value=0.01,
            value=float(configured_budget) if configured_budget is not None else 10.0,
            step=1.0, format="%.2f", disabled=configured_budget is not None,
        )
        if configured_budget is not None:
            st.caption("This workspace already has an approved budget. Changing it requires a separate budget approval.")
        approved = st.checkbox(
            "I approve this paid generation, its displayed inputs and the workspace budget.",
            key=f"approve_{plan['digest']}_{total_budget}", disabled=changed,
        )
        already_sent = st.session_state.get("slapz_submitted_plans", {}).get(plan["digest"])
        if already_sent:
            st.info(f"This plan has already been submitted as {already_sent}. Check the job below.")
        if st.button("Generate with Replicate · paid", type="primary", disabled=not approved or changed or bool(already_sent)):
            try:
                with st.spinner("Submitting the approved generation…"):
                    job = provider.submit(plan, approved_digest=plan["digest"], budget_usd=total_budget)
                st.session_state.setdefault("slapz_submitted_plans", {})[plan["digest"]] = job["job_id"]
                if job["status"] in {"unknown", "submitting"}:
                    st.warning(f"Submission outcome is uncertain for {job['job_id']}. Its reservation is retained. Check the job and Replicate dashboard before any new generation; this plan will not be resubmitted.")
                elif job["status"] in {"failed", "canceled"}:
                    st.error(f"Generation {job['job_id']} is {job['status']}. Its budget reservation remains recorded.")
                else:
                    st.success(f"Generation submitted: {job['job_id']}")
            except (ProviderError, OSError) as error:
                show_error(error)
    st.divider()
    st.subheader("Generation jobs")
    st.caption("Jobs and budget reservations persist locally across sessions. Status checks and downloads run only when clicked.")
    try:
        jobs = provider.list_jobs()
        budget = provider.budget_summary()
        with st.expander("Budget ledger"):
            st.json(budget)
        if not jobs:
            st.caption("No generation jobs yet.")
        for job in jobs:
            with st.expander(f"{job['status']} · {job['plan']['model_id']} · {job['job_id']}"):
                if job.get("prediction_id"):
                    st.caption(f"Replicate prediction: {job['prediction_id']}")
                if job.get("error"):
                    st.error(str(job["error"]))
                actions = st.columns(2)
                if actions[0].button("Refresh status", key=f"poll_{job['job_id']}"):
                    provider.poll(job["job_id"])
                    st.rerun()
                if actions[1].button("Retrieve generated files", key=f"fetch_{job['job_id']}", disabled=job["status"] != "succeeded"):
                    provider.fetch_result(job["job_id"])
                    st.rerun()
                for index, file in enumerate(job.get("files", [])):
                    path = local_output_path(file["path"], ROOT / "storage" / "studio" / "replicate")
                    media_type = file.get("media_type", "")
                    if media_type.startswith("image/"):
                        st.image(str(path), width="stretch")
                    elif media_type.startswith("video/"):
                        st.video(str(path))
                    elif media_type.startswith("audio/"):
                        st.audio(str(path))
                    if st.button("Add to scene library", key=f"use_{job['job_id']}_{index}"):
                        asset = store_upload(path.read_bytes(), path.name, WORKSPACE / "uploads")
                        asset["generated"] = True
                        ASSETS[asset["id"]] = asset
                        st.success(f"Added {asset['name']}. Select it as a scene background or audio track in Create.")
    except (ProviderError, ValueError, OSError) as error:
        show_error(error)


st.markdown('<div class="studio-label">SLAPZ / CONTENT STUDIO</div>', unsafe_allow_html=True)
st.title("Make the next post.")
st.markdown('<div class="studio-deck">Build photo carousels, slideshow videos and app demos in one place. Every export starts as a draft you can review.</div>', unsafe_allow_html=True)
create, recipes, generate, saved = st.tabs(["Create", "Recipes", "Replicate assets", "Save / import"])
with create:
    create_tab()
with recipes:
    recipes_tab()
with generate:
    generation_tab()
with saved:
    project_tab()
