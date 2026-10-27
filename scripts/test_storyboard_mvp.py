import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from freevideo_engine.agent.client import ApiError
from scripts import storyboard_mvp as storyboard


class FakeClient:
    def __init__(self, video, *, lose_first_reply=False, terminal="succeeded"):
        self.video = Path(video)
        self.lose_first_reply = lose_first_reply
        self.terminal = terminal
        self.submissions = []
        self.cancelled = []
        self.resumed = []
        self.jobs_by_ref = {}

    def capabilities(self):
        return {"api_version": "1", "build": "test-build", "ready": True}

    def schema(self):
        return {"version": "test-schema"}

    def guide(self):
        return {"version": "test-guide"}

    def upload(self, path):
        return {"file": "inputs/" + storyboard.sha256_file(path) + Path(path).suffix}

    def plan(self, spec):
        return {"geometry": {"width": spec["width"], "height": spec["height"]}, "warnings": []}

    def submit(self, spec):
        self.submissions.append(json.loads(json.dumps(spec)))
        job = self.jobs_by_ref.setdefault(spec["ref"], "job-%02d" % (len(self.jobs_by_ref) + 1))
        if self.lose_first_reply:
            self.lose_first_reply = False
            raise ApiError(0, "unreachable", "lost", stage="submission_unknown")
        return {"id": job, "status": "queued"}

    def job(self, job_id, wait=0):
        if self.terminal == "interrupt":
            raise KeyboardInterrupt
        if self.terminal == "failed":
            return {"id": job_id, "status": "failed", "error": {"code": "test_failure"}}
        return {"id": job_id, "status": "succeeded", "result": {"video": {"url": "/test.mp4"}}}

    def resume(self, job_id, *, ref=None):
        self.resumed.append((job_id, ref))
        self.terminal = "succeeded"
        return {"id": job_id, "status": "queued"}

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.terminal = "cancelled"
        return {"id": job_id, "status": "cancelled"}

    def download(self, view, target):
        shutil.copyfile(self.video, target)
        return Path(target)


class StoryboardMvpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("FFmpeg and ffprobe are required")
        cls.media_dir = Path(tempfile.mkdtemp(prefix="storyboard-mvp-media-"))
        cls.video = cls.media_dir / "shot.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x192:r=24:d=5",
            "-frames:v", "120", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.video),
        ], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_dir)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storyboard-mvp-test-")
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        (self.input / "frames").mkdir(parents=True)
        (self.input / "audio").mkdir()
        self.workdir = self.root / "work"

    def tearDown(self):
        self.temp.cleanup()

    def write_frame(self, shot, color):
        path = self.input / "frames" / (shot + ".png")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=%s:s=320x192" % color,
            "-frames:v", "1", str(path),
        ], check=True)
        return path

    def write_audio(self, shot, seconds=1.0, value=0):
        path = self.input / "audio" / (shot + ".wav")
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(int(seconds * 16000) * int(value).to_bytes(2, "little", signed=True))
        return path

    def write_manifest(self, count=2, *, audio_seconds=1.0):
        colors = ["red", "green", "yellow"]
        shots = []
        for index in range(count):
            shot = "s%02d" % (index + 1)
            self.write_frame(shot, colors[index])
            self.write_audio(shot, audio_seconds)
            shots.append({
                "id": shot,
                "first_frame": "frames/%s.png" % shot,
                "prompt": "Worker performs action %d in one continuous shot." % (index + 1),
                "seed": 100 + index,
                "narration": {"audio": "audio/%s.wav" % shot, "text": "第%d镜旁白。" % (index + 1)},
            })
        manifest = {
            "schema_version": 1,
            "project_id": "test-project",
            "style": "Same worker, same workshop, realistic style.",
            "render": {"width": 960, "height": 544, "seconds": 5, "quality": "light"},
            "export": {"width": 1280, "height": 720, "fps": 24},
            "shots": shots,
        }
        path = self.input / "storyboard.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def approve_current(self):
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        reviews = {"project_id": state["project_id"], "shots": {}}
        for shot_id, shot in state["shots"].items():
            reviews["shots"][shot_id] = {
                "status": "approved", "version": shot["version"], "sha256": shot["output_sha256"],
            }
        path = self.workdir / "review.json"
        path.write_text(json.dumps(reviews), encoding="utf-8")
        return path

    def test_validation_rejects_long_narration_before_client_use(self):
        manifest = self.write_manifest(audio_seconds=4.8)
        with self.assertRaisesRegex(storyboard.StoryboardError, "4.7"):
            storyboard.render(manifest, self.root / "missing-freevideo", self.workdir, client=object())
        self.assertFalse(self.workdir.exists())

    def test_unknown_submission_replays_identical_ref_once(self):
        manifest = self.write_manifest(count=1)
        client = FakeClient(self.video, lose_first_reply=True)
        state = storyboard.render(manifest, self.root, self.workdir, client=client)
        self.assertEqual(2, len(client.submissions))
        self.assertEqual(client.submissions[0], client.submissions[1])
        self.assertEqual("succeeded", state["shots"]["s01"]["status"])

    def test_restart_reuses_verified_video(self):
        manifest = self.write_manifest(count=1)
        client = FakeClient(self.video)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        self.assertEqual(1, len(client.submissions))

    def test_changed_first_frame_renders_only_that_shot(self):
        manifest = self.write_manifest(count=2)
        client = FakeClient(self.video)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        self.write_frame("s02", "black")
        state = storyboard.render(manifest, self.root, self.workdir, client=client)
        self.assertEqual(["test-project.s01.v1", "test-project.s02.v1", "test-project.s02.v2"],
                         [row["ref"] for row in client.submissions])
        self.assertEqual(1, state["shots"]["s01"]["version"])
        self.assertEqual(2, state["shots"]["s02"]["version"])

    def test_changed_narration_does_not_render_video(self):
        manifest = self.write_manifest(count=1)
        client = FakeClient(self.video)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        self.write_audio("s01", seconds=1.2, value=1)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        self.assertEqual(1, len(client.submissions))

    def test_failed_job_requires_explicit_retry(self):
        manifest = self.write_manifest(count=1)
        client = FakeClient(self.video, terminal="failed")
        with self.assertRaisesRegex(storyboard.StoryboardError, "retry-failed"):
            storyboard.render(manifest, self.root, self.workdir, client=client)
        with self.assertRaisesRegex(storyboard.StoryboardError, "retry-failed"):
            storyboard.render(manifest, self.root, self.workdir, client=client)
        storyboard.render(manifest, self.root, self.workdir, retry_failed=True, client=client)
        self.assertEqual(1, len(client.resumed))
        self.assertEqual(1, len(client.submissions))

    def test_interrupt_cancels_current_job_and_stops(self):
        manifest = self.write_manifest(count=2)
        client = FakeClient(self.video, terminal="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            storyboard.render(manifest, self.root, self.workdir, client=client)
        self.assertEqual(["job-01"], client.cancelled)
        self.assertEqual(1, len(client.submissions))

    def test_export_rejects_stale_review_after_frame_change(self):
        manifest = self.write_manifest(count=2)
        client = FakeClient(self.video)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        reviews = self.approve_current()
        self.write_frame("s02", "white")
        storyboard.render(manifest, self.root, self.workdir, only="s02", client=client)
        with self.assertRaisesRegex(storyboard.StoryboardError, "review"):
            storyboard.export_candidate(manifest, self.workdir, reviews)

    def test_export_produces_exact_frames_audio_and_subtitles(self):
        manifest = self.write_manifest(count=2)
        client = FakeClient(self.video)
        storyboard.render(manifest, self.root, self.workdir, client=client)
        reviews = self.approve_current()
        candidate = storyboard.export_candidate(manifest, self.workdir, reviews)
        video = storyboard.probe_media(candidate)
        self.assertEqual(240, video["video_frames"])
        self.assertTrue(video["has_audio"])
        self.assertTrue((self.workdir / "subtitles.srt").is_file())
        timing = json.loads((self.workdir / "timing.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(timing["export_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
