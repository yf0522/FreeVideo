#!/usr/bin/env python3
"""Render a reviewed storyboard with FreeVideo, then add narration and subtitles."""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from freevideo_engine.agent.client import ApiError, Client
from freevideo_engine.agent import schema as agent_schema


CLIENT_NAME = "yf0522-storyboard-mvp"
DONE = {"succeeded", "failed", "cancelled", "interrupted"}
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class StoryboardError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def workdir_lock(workdir):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stream = (workdir / ".storyboard-mvp.lock").open("a+b")
    try:
        try:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoryboardError("another storyboard render/export process is using this workdir") from None
        yield
    finally:
        stream.close()


def run(command):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise StoryboardError("required command is unavailable: %s" % command[0]) from None
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-1200:]
        raise StoryboardError("%s failed%s" % (command[0], ": " + detail if detail else "")) from None


def probe_media(path):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise StoryboardError("media file is missing or empty: %s" % path)
    result = run([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])
    try:
        report = json.loads(result.stdout)
        streams = report.get("streams") or []
        video = next((row for row in streams if row.get("codec_type") == "video"), None)
        audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
        duration = (audio or video or {}).get("duration") or (report.get("format") or {}).get("duration")
        frames = (video or {}).get("nb_read_frames") or (video or {}).get("nb_frames")
        return {
            "duration": float(duration) if duration not in (None, "N/A") else None,
            "has_video": video is not None,
            "has_audio": audio is not None,
            "width": int(video["width"]) if video and video.get("width") else None,
            "height": int(video["height"]) if video and video.get("height") else None,
            "video_frames": int(frames) if frames not in (None, "N/A") else None,
        }
    except (ValueError, TypeError, KeyError):
        raise StoryboardError("ffprobe returned invalid media metadata for %s" % path) from None


def _object(value, name, required, allowed):
    if not isinstance(value, dict):
        raise StoryboardError("%s must be an object" % name)
    missing = [key for key in required if key not in value]
    unknown = sorted(set(value) - set(allowed))
    if missing:
        raise StoryboardError("%s is missing: %s" % (name, ", ".join(missing)))
    if unknown:
        raise StoryboardError("%s has unknown fields: %s" % (name, ", ".join(unknown)))


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise StoryboardError("%s must be non-empty text" % name)
    return value.strip()


def _asset(root, value, name):
    relative = Path(_text(value, name))
    if relative.is_absolute() or ".." in relative.parts:
        raise StoryboardError("%s must stay inside the manifest directory" % name)
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise StoryboardError("%s resolves outside the manifest directory" % name) from None
    if not path.is_file():
        raise StoryboardError("%s does not exist: %s" % (name, path))
    return path


def validate_manifest(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except FileNotFoundError:
        raise StoryboardError("manifest does not exist: %s" % path) from None
    except (OSError, ValueError):
        raise StoryboardError("manifest is not valid UTF-8 JSON: %s" % path) from None
    _object(value, "manifest", ("schema_version", "project_id", "style", "render", "export", "shots"),
            ("schema_version", "project_id", "style", "render", "export", "shots"))
    if value["schema_version"] != 1:
        raise StoryboardError("schema_version must be 1")
    project_id = _text(value["project_id"], "project_id")
    if not SLUG.fullmatch(project_id):
        raise StoryboardError("project_id must match the FreeVideo slug format")
    value["style"] = _text(value["style"], "style")

    render = value["render"]
    _object(render, "render", ("width", "height", "seconds", "quality"),
            ("width", "height", "seconds", "quality"))
    for key in ("width", "height"):
        number = render[key]
        if type(number) is not int or not 256 <= number <= 4096 or number % 32:
            raise StoryboardError("render.%s must be 256..4096 and divisible by 32" % key)
    if render["seconds"] != 5:
        raise StoryboardError("render.seconds must be 5 for storyboard MVP")
    if render["quality"] not in agent_schema.QUALITY:
        raise StoryboardError("render.quality is not supported by the current FreeVideo schema")

    output = value["export"]
    _object(output, "export", ("width", "height", "fps"), ("width", "height", "fps"))
    for key in ("width", "height"):
        if type(output[key]) is not int or not 64 <= output[key] <= 4096 or output[key] % 2:
            raise StoryboardError("export.%s must be an even integer from 64 to 4096" % key)
    if output["fps"] != 24:
        raise StoryboardError("export.fps must be 24 for storyboard MVP")

    shots = value["shots"]
    if not isinstance(shots, list) or not 1 <= len(shots) <= 12:
        raise StoryboardError("shots must contain 1 to 12 shots")
    root, seen = path.parent, set()
    for index, shot in enumerate(shots):
        name = "shots[%d]" % index
        _object(shot, name, ("id", "first_frame", "prompt", "seed", "narration"),
                ("id", "first_frame", "prompt", "seed", "narration"))
        shot_id = _text(shot["id"], name + ".id")
        if not SLUG.fullmatch(shot_id) or shot_id in seen:
            raise StoryboardError("%s.id must be a unique FreeVideo slug" % name)
        if len("%s.%s.v999999" % (project_id, shot_id)) > 128:
            raise StoryboardError("%s.id makes the stable ref longer than 128 characters" % name)
        seen.add(shot_id)
        shot["prompt"] = _text(shot["prompt"], name + ".prompt")
        if type(shot["seed"]) is not int or not 0 <= shot["seed"] <= 2**53 - 1:
            raise StoryboardError("%s.seed must be an integer from 0 to 2^53-1" % name)
        frame = _asset(root, shot["first_frame"], name + ".first_frame")
        frame_probe = probe_media(frame)
        if (not frame_probe["has_video"] or frame_probe["has_audio"] or not frame_probe["width"]
                or not frame_probe["height"] or frame_probe.get("video_frames") not in (None, 1)):
            raise StoryboardError("%s.first_frame is not a decodable image" % name)
        shot["_first_frame"] = frame
        shot["_first_frame_sha256"] = sha256_file(frame)
        shot["_first_frame_probe"] = frame_probe
        narration = shot["narration"]
        if narration is None:
            continue
        _object(narration, name + ".narration", ("audio", "text"), ("audio", "text"))
        narration["text"] = _text(narration["text"], name + ".narration.text").replace("\r", " ").replace("\n", " ")
        audio = _asset(root, narration["audio"], name + ".narration.audio")
        if audio.suffix.lower() != ".wav":
            raise StoryboardError("%s.narration.audio must be a WAV file" % name)
        audio_probe = probe_media(audio)
        duration = audio_probe["duration"]
        if not audio_probe["has_audio"] or duration is None or duration <= 0:
            raise StoryboardError("%s.narration.audio is not decodable audio" % name)
        if duration > 4.7 + 0.001:
            raise StoryboardError("%s narration is %.3fs; maximum is 4.7s" % (shot_id, duration))
        narration["_audio"] = audio
        narration["_audio_sha256"] = sha256_file(audio)
        narration["_audio_probe"] = audio_probe
    value["_path"] = path
    value["_sha256"] = sha256_bytes(raw)
    return value


def frozen_prompt(manifest, shot):
    return ("integrated_multimodal_description: [Shot 1] %s. %s The subject remains silent with lips closed. "
            "No subtitles, no on-screen text.\n\n"
            "overall_soundscape: N/A\n\nnon_diegetic_music: N/A" % (manifest["style"], shot["prompt"]))


def service_identity(capabilities, schema, guide):
    stable = {key: capabilities.get(key) for key in
              ("api_version", "freevideo_version", "build") if capabilities.get(key) is not None}
    device = capabilities.get("device")
    if isinstance(device, dict):
        stable["device_backend"] = device.get("backend")
    stable["schema_sha256"] = sha256_bytes(canonical(schema).encode())
    stable["guide_sha256"] = sha256_bytes(canonical(guide).encode())
    return stable


def visual_fingerprint(manifest, shot, service):
    value = {
        "first_frame_sha256": shot["_first_frame_sha256"],
        "prompt": frozen_prompt(manifest, shot),
        "seed": shot["seed"],
        "render": manifest["render"],
        "service": service,
    }
    return sha256_bytes(canonical(value).encode())


def _state_path(workdir):
    return Path(workdir) / "state.json"


def load_state(workdir):
    path = _state_path(workdir)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("shots"), dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise StoryboardError("state.json is unreadable; preserve it and repair or use another workdir") from None


def save_state(workdir, state):
    state["updated_at"] = now()
    atomic_json(_state_path(workdir), state)


def git_commit():
    root = Path(__file__).resolve().parents[1]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def error_view(error):
    if isinstance(error, ApiError):
        return {"code": error.code, "message": error.message, "details": error.details}
    return {"code": type(error).__name__, "message": str(error)}


def shot_path(workdir, record):
    return Path(workdir) / record["output_path"]


def cached_video_valid(workdir, record):
    try:
        path = shot_path(workdir, record)
        if not record.get("output_sha256") or sha256_file(path) != record["output_sha256"]:
            return False
        probe = probe_media(path)
        return bool(probe["has_video"] and probe.get("video_frames") is not None
                    and probe["video_frames"] >= 120)
    except (OSError, StoryboardError):
        return False


def _new_record(workdir, old, shot_id, fingerprint, request, plan, redo):
    version = int((old or {}).get("version", 0)) + 1
    same_input_attempt = int((old or {}).get("visual_attempt", 0)) + 1 if redo else 1
    if same_input_attempt > 2:
        raise StoryboardError("%s already used its one allowed creative redo; change the frame, prompt, seed, or camera" % shot_id)
    history = list((old or {}).get("history") or [])
    if old:
        history.append({key: old.get(key) for key in (
            "version", "visual_attempt", "visual_fingerprint", "ref", "job_id", "status", "error",
            "output_path", "output_sha256", "probe", "timing", "finished_at")})
    relative = Path("shots") / shot_id / ("v%d.mp4" % version)
    return {
        "version": version,
        "visual_attempt": same_input_attempt,
        "visual_fingerprint": fingerprint,
        "ref": "%s.%s.v%d" % (request["project"], shot_id, version),
        "request": request,
        "plan": plan,
        "job_id": None,
        "status": "prepared",
        "submit_replays": 0,
        "resume_count": 0,
        "error": None,
        "output_path": str(relative),
        "output_sha256": None,
        "probe": None,
        "timing": {},
        "history": history[-20:],
        "created_at": now(),
    }


def _download(client, workdir, record, view):
    result = view.get("result") or {}
    target = shot_path(workdir, record)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".download")
    temporary.unlink(missing_ok=True)
    started = time.monotonic()
    try:
        video = result.get("video")
        if isinstance(video, dict) and video.get("url"):
            client.download(video, temporary)
        elif result.get("library_id"):
            client.asset(result["library_id"], temporary)
        else:
            raise StoryboardError("succeeded job has no downloadable video")
        probe = probe_media(temporary)
        if not probe["has_video"] or probe.get("video_frames") is None or probe["video_frames"] < 120:
            raise StoryboardError("downloaded shot has fewer than 120 decodable video frames")
        digest = sha256_file(temporary)
        os.replace(temporary, target)
        record.update(output_sha256=digest, probe=probe, result=result)
        record["timing"]["download_seconds"] = round(time.monotonic() - started, 3)
    finally:
        temporary.unlink(missing_ok=True)


def _submit(client, record):
    request = dict(record["request"], ref=record["ref"])
    record["request"] = request
    try:
        return client.submit(request)
    except ApiError as error:
        # A transport failure on POST /jobs has an unknown outcome. The stable ref makes one replay safe.
        if error.code != "unreachable":
            raise
        record["status"] = "submission_unknown"
        record["submit_replays"] += 1
        return client.submit(request)


def _finish_job(client, workdir, state, shot_id, record, retry_failed, wait_timeout):
    if record["status"] in ("failed", "interrupted"):
        if not retry_failed:
            raise StoryboardError("%s is %s; rerun with --retry-failed" % (shot_id, record["status"]))
        resume_ref = "%s.resume%d" % (record["ref"], record["resume_count"] + 1)
        view = client.resume(record["job_id"], ref=resume_ref)
        record["resume_count"] += 1
        record["status"] = view.get("status", "queued")
        record["error"] = None
        save_state(workdir, state)
    elif record["status"] == "cancelled":
        raise StoryboardError("%s was cancelled and will not resume automatically" % shot_id)

    if not record.get("job_id"):
        started = time.monotonic()
        try:
            job = _submit(client, record)
        except Exception as error:
            record["error"] = error_view(error)
            save_state(workdir, state)
            raise
        record["job_id"] = job["id"]
        record["status"] = job.get("status", "queued")
        record["timing"]["submit_seconds"] = round(time.monotonic() - started, 3)
        save_state(workdir, state)

    started = time.monotonic()
    try:
        while True:
            view = client.job(record["job_id"], wait=25)
            record["status"] = view["status"]
            record["error"] = view.get("error")
            save_state(workdir, state)
            if view["status"] in DONE:
                break
            if wait_timeout is not None and time.monotonic() - started >= wait_timeout:
                raise StoryboardError("%s is still %s; no new job was submitted" % (shot_id, view["status"]))
    except KeyboardInterrupt:
        try:
            view = client.cancel(record["job_id"])
            record["status"] = view.get("status", "cancelling")
            while record["status"] not in DONE:
                view = client.job(record["job_id"], wait=25)
                record["status"] = view["status"]
            record["error"] = view.get("error")
        except Exception as error:
            record["error"] = error_view(error)
        save_state(workdir, state)
        raise
    except Exception as error:
        if not isinstance(error, StoryboardError):
            record["error"] = error_view(error)
        save_state(workdir, state)
        raise
    record["timing"]["generation_seconds"] = round(time.monotonic() - started, 3)
    if view["status"] in ("failed", "interrupted"):
        raise StoryboardError("%s is %s; rerun with --retry-failed" % (shot_id, view["status"]))
    if view["status"] == "cancelled":
        raise StoryboardError("%s was cancelled and will not resume automatically" % shot_id)
    if "cold_start" not in state["timing"]:
        state["timing"]["cold_start"] = {
            "shot_id": shot_id,
            "generation_seconds": record["timing"]["generation_seconds"],
            "note": "first completed generation in this workdir; includes queue and any model warm-up",
        }
    try:
        _download(client, workdir, record, view)
    except Exception as error:
        record["error"] = error_view(error)
        save_state(workdir, state)
        raise
    record["finished_at"] = now()
    save_state(workdir, state)


def render(manifest_path, freevideo_root, workdir, *, only=None, retry_failed=False, redo=False,
           wait_timeout=None, client=None):
    manifest = validate_manifest(manifest_path)
    ids = [shot["id"] for shot in manifest["shots"]]
    if only is not None and only not in ids:
        raise StoryboardError("--only is not a shot id in the manifest: %s" % only)
    if redo and only is None:
        raise StoryboardError("--redo requires --only so one shot is redone explicitly")
    workdir = Path(workdir).expanduser().resolve()
    with workdir_lock(workdir):
        client = client or Client(root=freevideo_root, name=CLIENT_NAME)
        connected_at = time.monotonic()
        capabilities = client.capabilities()
        if capabilities.get("ready") is False:
            raise StoryboardError("FreeVideo is not ready: %s" % (capabilities.get("problem") or "unknown problem"))
        schema_view, guide_view = client.schema(), client.guide()
        service = service_identity(capabilities, schema_view, guide_view)
        connect_seconds = round(time.monotonic() - connected_at, 3)
        state = load_state(workdir)
        if state and state.get("project_id") != manifest["project_id"]:
            raise StoryboardError("workdir belongs to project %s" % state.get("project_id"))
        if state is None:
            state = {
                "schema_version": 1,
                "project_id": manifest["project_id"],
                "created_at": now(),
                "fork_commit": git_commit(),
                "service": service,
                "shots": {},
                "timing": {},
            }
        state.update(manifest_path=str(manifest["_path"]), manifest_sha256=manifest["_sha256"], service=service)
        state["timing"]["service_connect_seconds"] = connect_seconds
        save_state(workdir, state)

        for shot in manifest["shots"]:
            shot_id = shot["id"]
            if only is not None and shot_id != only:
                continue
            fingerprint = visual_fingerprint(manifest, shot, service)
            record = state["shots"].get(shot_id)
            changed = not record or record.get("visual_fingerprint") != fingerprint
            force_redo = bool(redo and shot_id == only)
            if record and not changed and not force_redo and record.get("status") == "succeeded" and cached_video_valid(workdir, record):
                continue
            if changed or force_redo:
                uploaded = client.upload(shot["_first_frame"])
                request = {
                    "kind": "generate",
                    "label": shot_id,
                    "project": manifest["project_id"],
                    "prompt": frozen_prompt(manifest, shot),
                    "width": manifest["render"]["width"],
                    "height": manifest["render"]["height"],
                    "seconds": manifest["render"]["seconds"],
                    "quality": manifest["render"]["quality"],
                    "seed": shot["seed"],
                    "media": {"first": {"file": uploaded["file"]}},
                }
                planned = client.plan(request)
                record = _new_record(workdir, record, shot_id, fingerprint, request, planned, force_redo)
                record["request"]["ref"] = record["ref"]
                state["shots"][shot_id] = record
                save_state(workdir, state)
            elif record.get("status") == "succeeded" and not cached_video_valid(workdir, record):
                record["status"] = "download_required"
            _finish_job(client, workdir, state, shot_id, record, retry_failed, wait_timeout)
        return state


def _review_rows(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StoryboardError("review file is missing or invalid JSON: %s" % path) from None
    if not isinstance(value, dict) or not isinstance(value.get("shots"), dict):
        raise StoryboardError("review file must contain a shots object")
    return value


def _srt_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    whole, milliseconds = divmod(milliseconds, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, whole, milliseconds)


def _font():
    choices = [
        (Path("/System/Library/Fonts/PingFang.ttc"), "PingFang SC"),
        (Path("/System/Library/Fonts/STHeiti Medium.ttc"), "Heiti SC"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), "Noto Sans CJK SC"),
    ]
    for path, name in choices:
        if path.is_file():
            return path.parent, name
    raise StoryboardError("no supported Chinese subtitle font was found")


def _filter_path(path):
    return str(Path(path).resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _write_subtitles(manifest, target):
    rows = []
    for start, end, text in _subtitle_cues(manifest):
        rows.append("%d\n%s --> %s\n%s\n" %
                    (len(rows) + 1, _srt_time(start), _srt_time(end), text))
    Path(target).write_text("\n".join(rows), encoding="utf-8")


def _subtitle_cues(manifest):
    rows = []
    for index, shot in enumerate(manifest["shots"]):
        narration = shot["narration"]
        if narration is not None:
            start = index * 5
            rows.append((start, start + narration["_audio_probe"]["duration"], narration["text"]))
    return rows


def _ffmpeg_has_filter(name):
    try:
        value = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], check=True,
                               capture_output=True, text=True).stdout
        return any(len(parts) > 1 and parts[1] == name
                   for parts in (line.split() for line in value.splitlines()))
    except (OSError, subprocess.CalledProcessError):
        return False


def _caption_png(text, width, height, font_name, target):
    """Use macOS' installed Chinese font when FFmpeg was built without libass."""
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        raise StoryboardError("FFmpeg lacks the subtitles filter and no macOS Swift fallback is available")
    source = Path(target).with_suffix(".swift")
    source.write_text(r'''import AppKit
let a = CommandLine.arguments
let output = a[1], width = Int(a[2])!, height = Int(a[3])!, fontName = a[4], text = a[5]
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor.clear.setFill(); NSRect(x: 0, y: 0, width: width, height: height).fill()
NSColor.black.withAlphaComponent(0.68).setFill()
NSBezierPath(roundedRect: NSRect(x: 64, y: 22, width: width - 128, height: 86),
    xRadius: 12, yRadius: 12).fill()
let paragraph = NSMutableParagraphStyle(); paragraph.alignment = .center
let attrs: [NSAttributedString.Key: Any] = [
    .font: NSFont(name: fontName, size: 38) ?? NSFont.systemFont(ofSize: 38),
    .foregroundColor: NSColor.white,
    .paragraphStyle: paragraph]
(text as NSString).draw(with: NSRect(x: 80, y: 36, width: width - 160, height: 62),
    options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attrs)
NSGraphicsContext.restoreGraphicsState()
let data = bitmap.representation(using: .png, properties: [:])!
try! data.write(to: URL(fileURLWithPath: output))
''', encoding="utf-8")
    try:
        run([swift, str(source), str(target), str(width), str(height), font_name, text])
    finally:
        source.unlink(missing_ok=True)


def export_fingerprint(manifest, state, font_name):
    shots = []
    for shot in manifest["shots"]:
        record = state["shots"][shot["id"]]
        narration = shot["narration"]
        shots.append({
            "id": shot["id"], "version": record["version"], "video": record["output_sha256"],
            "audio": narration["_audio_sha256"] if narration else None,
            "text": narration["text"] if narration else None,
            "duration": narration["_audio_probe"]["duration"] if narration else None,
        })
    return sha256_bytes(canonical({"shots": shots, "export": manifest["export"], "font": font_name}).encode())


def export_candidate(manifest_path, workdir, reviews_path):
    manifest = validate_manifest(manifest_path)
    workdir = Path(workdir).expanduser().resolve()
    with workdir_lock(workdir):
        state = load_state(workdir)
        if not state or state.get("project_id") != manifest["project_id"]:
            raise StoryboardError("workdir has no state for this project")
        reviews = _review_rows(reviews_path)
        if reviews.get("project_id") not in (None, manifest["project_id"]):
            raise StoryboardError("review file belongs to another project")
        videos = []
        for shot in manifest["shots"]:
            shot_id = shot["id"]
            record = state["shots"].get(shot_id)
            if not record or record.get("status") != "succeeded":
                raise StoryboardError("%s has no succeeded current video" % shot_id)
            expected = visual_fingerprint(manifest, shot, state["service"])
            if record.get("visual_fingerprint") != expected or not cached_video_valid(workdir, record):
                raise StoryboardError("%s video version or file hash is stale" % shot_id)
            review = reviews["shots"].get(shot_id)
            if not review or review.get("status") != "approved" or review.get("version") != record["version"] or review.get("sha256") != record["output_sha256"]:
                raise StoryboardError("%s review does not approve the current version and hash" % shot_id)
            videos.append(shot_path(workdir, record))

        font_dir, font_name = _font()
        fingerprint = export_fingerprint(manifest, state, font_name)
        candidate = workdir / "candidate.mp4"
        previous = state.get("export") or {}
        if (previous.get("fingerprint") == fingerprint and previous.get("sha256")
                and candidate.is_file() and (workdir / "subtitles.srt").is_file()
                and (workdir / "timing.json").is_file()
                and sha256_file(candidate) == previous["sha256"]):
            return candidate

        subtitles = workdir / "subtitles.srt"
        _write_subtitles(manifest, subtitles)
        width, height = manifest["export"]["width"], manifest["export"]["height"]
        count, filters, audio_inputs = len(videos), [], []
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for video in videos:
            command += ["-i", str(video)]
        next_input = count
        for index, shot in enumerate(manifest["shots"]):
            command_audio = shot["narration"]
            if command_audio:
                command += ["-i", str(command_audio["_audio"])]
                audio_inputs.append(next_input)
                next_input += 1
            else:
                audio_inputs.append(None)
        caption_inputs = []
        if subtitles.stat().st_size and not _ffmpeg_has_filter("subtitles"):
            caption_dir = workdir / "captions"
            caption_dir.mkdir(exist_ok=True)
            for index, (start, end, text) in enumerate(_subtitle_cues(manifest), 1):
                picture = caption_dir / ("cue-%03d.png" % index)
                _caption_png(text, width, height, font_name, picture)
                command += ["-loop", "1", "-i", str(picture)]
                caption_inputs.append((next_input, start, end))
                next_input += 1
        for index, shot in enumerate(manifest["shots"]):
            filters.append("[%d:v]trim=start_frame=0:end_frame=120,setpts=PTS-STARTPTS,fps=24," % index
                           + "scale=%d:%d:force_original_aspect_ratio=decrease," % (width, height)
                           + "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1[v%d]" % (width, height, index))
        filters.append("%sconcat=n=%d:v=1:a=0[vcat]" % ("".join("[v%d]" % i for i in range(count)), count))
        for index, input_index in enumerate(audio_inputs):
            if input_index is None:
                filters.append("anullsrc=r=48000:cl=stereo:d=5[a%d]" % index)
            else:
                filters.append("[%d:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo," % input_index
                               + "apad=pad_dur=5,atrim=duration=5,asetpts=PTS-STARTPTS[a%d]" % index)
        filters.append("%sconcat=n=%d:v=0:a=1[acat]" % ("".join("[a%d]" % i for i in range(count)), count))
        if subtitles.stat().st_size and not caption_inputs:
            subtitle_filter = ("subtitles=filename='%s':fontsdir='%s':force_style='FontName=%s,FontSize=32,"
                               "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=0,MarginV=40'" %
                               (_filter_path(subtitles), _filter_path(font_dir), font_name))
            filters.append("[vcat]%s[vout]" % subtitle_filter)
        elif caption_inputs:
            current = "vcat"
            for index, (input_index, start, end) in enumerate(caption_inputs):
                output_name = "vout" if index == len(caption_inputs) - 1 else "vsub%d" % index
                filters.append("[%s][%d:v]overlay=0:0:enable='between(t,%.3f,%.3f)'[%s]" %
                               (current, input_index, start, end, output_name))
                current = output_name
        else:
            filters.append("[vcat]null[vout]")
        temporary = candidate.with_name(candidate.name + ".part")
        temporary.unlink(missing_ok=True)
        started = time.monotonic()
        try:
            command += [
                "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[acat]",
                "-frames:v", str(count * 120), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                "-f", "mp4", str(temporary),
            ]
            run(command)
            probe = probe_media(temporary)
            if (probe.get("video_frames") != count * 120 or not probe["has_audio"]
                    or probe["width"] != width or probe["height"] != height):
                raise StoryboardError("candidate media verification failed")
            digest = sha256_file(temporary)
            os.replace(temporary, candidate)
        finally:
            temporary.unlink(missing_ok=True)
        export_seconds = round(time.monotonic() - started, 3)
        state["export"] = {
            "fingerprint": fingerprint, "path": "candidate.mp4", "sha256": digest,
            "probe": probe, "reviews": str(Path(reviews_path).resolve()), "created_at": now(),
        }
        state.update(manifest_path=str(manifest["_path"]), manifest_sha256=manifest["_sha256"])
        save_state(workdir, state)
        timing = {
            "service_connect_seconds": state.get("timing", {}).get("service_connect_seconds"),
            "cold_start": state.get("timing", {}).get("cold_start"),
            "shots": {shot_id: state["shots"][shot_id].get("timing", {}) for shot_id in state["shots"]},
            "previous_attempts": {
                shot_id: [{"version": row.get("version"), "timing": row.get("timing")}
                          for row in state["shots"][shot_id].get("history", []) if row.get("timing")]
                for shot_id in state["shots"] if any(row.get("timing") for row in state["shots"][shot_id].get("history", []))
            },
            "export_seconds": export_seconds,
        }
        atomic_json(workdir / "timing.json", timing)
        return candidate


def status(workdir, freevideo_root=None, *, client=None):
    workdir = Path(workdir).expanduser().resolve()
    state = load_state(workdir)
    if not state:
        raise StoryboardError("workdir has no state.json")
    result = {"project_id": state.get("project_id"), "updated_at": state.get("updated_at"), "shots": {}}
    if client is None and freevideo_root is not None:
        client = Client(root=freevideo_root, name=CLIENT_NAME)
    for shot_id, record in state["shots"].items():
        row = {
            "version": record.get("version"), "ref": record.get("ref"), "job_id": record.get("job_id"),
            "status": record.get("status"), "error": record.get("error"),
            "file_valid": cached_video_valid(workdir, record) if record.get("output_path") else False,
        }
        if client is not None and record.get("job_id") and record.get("status") not in DONE:
            try:
                remote = client.job(record["job_id"])
                row["remote_status"] = remote.get("status")
                row["remote_error"] = remote.get("error")
            except Exception as error:
                row["remote_error"] = error_view(error)
        result["shots"][shot_id] = row
    result["export"] = state.get("export")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate all local storyboard inputs")
    validate.add_argument("--manifest", required=True, type=Path)
    render_parser = commands.add_parser("render", help="render missing or changed shots")
    render_parser.add_argument("--manifest", required=True, type=Path)
    render_parser.add_argument("--freevideo-root", required=True, type=Path)
    render_parser.add_argument("--workdir", required=True, type=Path)
    render_parser.add_argument("--only")
    render_parser.add_argument("--retry-failed", action="store_true")
    render_parser.add_argument("--redo", action="store_true", help="redo --only once without changing its inputs")
    render_parser.add_argument("--wait-timeout", type=float)
    status_parser = commands.add_parser("status", help="show saved and live job status")
    status_parser.add_argument("--workdir", required=True, type=Path)
    status_parser.add_argument("--freevideo-root", type=Path)
    export_parser = commands.add_parser("export", help="verify reviews and create candidate.mp4")
    export_parser.add_argument("--manifest", required=True, type=Path)
    export_parser.add_argument("--workdir", required=True, type=Path)
    export_parser.add_argument("--reviews", required=True, type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            manifest = validate_manifest(args.manifest)
            print(json.dumps({"valid": True, "project_id": manifest["project_id"],
                              "shots": len(manifest["shots"])}, ensure_ascii=False))
        elif args.command == "render":
            state = render(args.manifest, args.freevideo_root, args.workdir, only=args.only,
                           retry_failed=args.retry_failed, redo=args.redo, wait_timeout=args.wait_timeout)
            print(json.dumps(status(args.workdir), ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(status(args.workdir, args.freevideo_root), ensure_ascii=False, indent=2))
        else:
            print(export_candidate(args.manifest, args.workdir, args.reviews))
        return 0
    except KeyboardInterrupt:
        print("cancel requested; current state was saved", file=sys.stderr)
        return 130
    except (StoryboardError, ApiError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
