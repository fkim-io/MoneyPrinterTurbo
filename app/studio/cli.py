"""Local composition and explicitly approved generation for Slapz Studio."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _read_json(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def load_project(path: str) -> dict:
    """CLI paths are deliberately local, relative to the project JSON file."""
    project = _read_json(path)
    directory = Path(path).resolve().parent
    scenes = project.get("scenes", [])
    if not isinstance(scenes, list) or not all(isinstance(scene, dict) for scene in scenes):
        raise ValueError("Project scenes must be an ordered list of objects.")

    def local_path(value):
        if not isinstance(value, str) or "://" in value:
            raise ValueError("Project media must use local file paths.")
        return str((directory / Path(value).expanduser()).resolve())

    for scene in scenes:
        if scene.get("source"):
            scene["source"] = local_path(scene["source"])
    for key in ("narration_file", "music_file"):
        if project.get(key):
            project[key] = local_path(project[key])
    return project


def starter_project(format_name: str, size: str = "portrait") -> dict:
    scenes = [
        {
            "headline": "your words.\nyour voice.",
            "body": "start with a beat. make it yours.",
            "layout": "boxed",
            "source_kind": "color",
            "duration": 4,
        },
        {
            "headline": "describe the beat.",
            "body": "give your idea somewhere to start.",
            "layout": "paragraph",
            "source_kind": "color",
            "duration": 4,
        },
        {
            "headline": "record your take.",
            "body": "the voice on the record is yours.",
            "layout": "top",
            "source_kind": "color",
            "duration": 4,
        },
    ]
    return {
        "title": "your words, your voice",
        "creative_id": "slapz-voice-001",
        "format": format_name,
        "size": size,
        "scenes": scenes[:1] if format_name == "image" else scenes,
    }


def _emit(value: dict, output: str | None = None) -> None:
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
        print(destination.resolve())
    else:
        print(encoded, end="")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Write an editable starter project; no network.")
    new.add_argument("--format", choices=["image", "carousel", "slideshow", "reel"], default="carousel")
    new.add_argument("--size", choices=["portrait", "feed", "square"], default="feed")
    new.add_argument("--output", required=True)
    validate = commands.add_parser("validate", help="Validate a local project without rendering.")
    validate.add_argument("project")
    render = commands.add_parser("render", help="Render a project locally; no paid APIs.")
    render.add_argument("project")
    render.add_argument("--output", required=True)
    render.add_argument("--captions-srt", help="Optional local SRT file; replaces the project's timed captions.")
    commands.add_parser("schema", help="Print the project JSON schema.")
    commands.add_parser("catalog", help="List the configurable Replicate model catalog.")
    handoff = commands.add_parser("handoff", help="Prepare a verified local HQ draft; never publish.")
    handoff.add_argument("render_dir")
    handoff.add_argument("--platform", choices=["instagram", "tiktok"], default="instagram")
    handoff.add_argument("--account", default="@getslapz")
    handoff.add_argument("--caption", default="")
    handoff.add_argument("--campaign", default="slapz-content")
    handoff.add_argument("--destination", default="https://slapz.ai/")
    handoff.add_argument("--ai-generated", action="store_true")
    handoff.add_argument("--output", required=True)
    plan = commands.add_parser("plan-generation", help="Create a generation plan without submitting it.")
    plan.add_argument("--model", required=True)
    plan.add_argument("--model-spec", help="Optional JSON model spec with a reviewed input_schema for a custom Replicate model.")
    plan.add_argument("--inputs", required=True, help="JSON file with the selected model's inputs.")
    plan.add_argument("--max-cost-usd", required=True, type=float, help="Your reviewed reservation; not a provider invoice guarantee.")
    plan.add_argument("--output", required=True)
    submit = commands.add_parser("generate", help="Submit an explicitly approved PAID generation plan.")
    submit.add_argument("plan")
    submit.add_argument("--approve-digest", required=True, help="Exact digest from the reviewed plan.")
    submit.add_argument("--budget-usd", required=True, type=float, help="Persistent total reservation limit.")
    submit.add_argument("--workspace", default="storage/studio/replicate")
    budget = commands.add_parser("budget", help="Inspect or explicitly change the local reservation limit; never submits a job.")
    budget.add_argument("--set-limit-usd", type=float)
    budget.add_argument("--approve-change", action="store_true")
    budget.add_argument("--workspace", default="storage/studio/replicate")
    reconcile = commands.add_parser("reconcile", help="Recover an unknown submission using a known provider prediction ID; GET only.")
    reconcile.add_argument("job_id")
    reconcile.add_argument("--prediction-id", required=True)
    reconcile.add_argument("--workspace", default="storage/studio/replicate")
    for command in ("jobs", "poll", "fetch"):
        sub = commands.add_parser(command, help={"jobs": "List existing generation jobs.", "poll": "Refresh one known job; never resubmit.", "fetch": "Save output of a completed job."}[command])
        if command != "jobs":
            sub.add_argument("job_id")
        sub.add_argument("--workspace", default="storage/studio/replicate")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "new":
            _emit(starter_project(args.format, args.size), args.output)
        elif args.command in {"schema", "validate", "render"}:
            from app.studio import StudioProject, render_project

            if args.command == "schema":
                _emit(StudioProject.model_json_schema())
            else:
                project_data = load_project(args.project)
                if args.command == "render" and args.captions_srt:
                    from app.studio.captions import parse_srt

                    project_data["captions"] = parse_srt(Path(args.captions_srt).read_text(encoding="utf-8-sig"))
                project = StudioProject.model_validate(project_data)
                if args.command == "render":
                    _emit(render_project(project, Path(args.output).resolve()))
                else:
                    _emit({"valid": True, "format": project.format, "scenes": len(project.scenes)})
        elif args.command == "handoff":
            from app.studio.handoff import build_handoff

            _emit(build_handoff(args.render_dir, args.platform, args.caption, args.campaign, args.destination, args.ai_generated, args.account), args.output)
        else:
            from app.studio.providers import ReplicateProvider, load_catalog, plan_generation

            if args.command == "catalog":
                _emit(load_catalog())
            elif args.command == "plan-generation":
                _emit(plan_generation(args.model, _read_json(args.inputs), args.max_cost_usd, model_spec=_read_json(args.model_spec) if args.model_spec else None), args.output)
            else:
                provider = ReplicateProvider(args.workspace)
                if args.command == "generate":
                    _emit(provider.submit(_read_json(args.plan), args.approve_digest, args.budget_usd))
                elif args.command == "jobs":
                    _emit({"jobs": provider.list_jobs(), "budget": provider.budget_summary()})
                elif args.command == "poll":
                    _emit(provider.poll(args.job_id))
                elif args.command == "reconcile":
                    _emit(provider.reconcile(args.job_id, args.prediction_id))
                elif args.command == "budget":
                    if args.set_limit_usd is not None:
                        provider.set_budget_limit(args.set_limit_usd, approved=args.approve_change)
                    _emit(provider.budget_summary())
                else:
                    _emit(provider.fetch_result(args.job_id))
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Studio: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
