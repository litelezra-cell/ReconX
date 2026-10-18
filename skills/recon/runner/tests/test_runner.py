"""Unit tests that need no models, no memguard install and no network. Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNNER))

_STATE = tempfile.mkdtemp(prefix="witness-test-state-")
os.environ["MEMGUARD_STATE_DIR"] = _STATE  # tests never touch the real machine ledger

import witness_guard as guard  # noqa: E402
import witness_worker as worker  # noqa: E402
from runner_config import RunnerRefused  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="witness-test-"))
        self.root = (self.tmp / "target").resolve()
        self.root.mkdir()
        self.outside = (self.tmp / "outside").resolve()
        self.outside.mkdir()
        self.python = sys.executable
        self.model = self.root / "model.bin"
        self.model.write_bytes(b"x")
        self.other = self.root / "other.bin"
        self.other.write_bytes(b"y")
        self.config = {"_path": str(self.tmp / "runner.json"), "stages": {
            "ocr": {"python": self.python, "budget_bytes": 1 << 30, "models": {}},
            "vlm": {"python": self.python, "budget_bytes": 45 << 30, "models": {"model_path": str(self.model)}}}}
        (self.tmp / "runner.json").write_text(json.dumps(self.config))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manifest(self, name="ocr-manifest.json", **data):
        path = self.root / name
        path.write_text(json.dumps(data))
        return path

    def request(self, manifest, stage="ocr", **extra):
        return {"stage": stage, "manifest_path": str(manifest),
                "output_path": str(self.root / f"x-{stage}-raw.json"), **extra}


class PathValidation(Base):
    def test_accepts_inside_paths_and_derives_guard_path(self):
        frame = self.root / "f.png"
        frame.write_bytes(b"png")
        req = guard.validate_request(self.request(self.manifest(frames=[{"path": str(frame), "t": 0}])),
                                     self.config, self.root)
        self.assertTrue(req["guard_path"].endswith("x-ocr-guard.json"))
        self.assertEqual(req["estimated_bytes"], 1 << 30)

    def test_refuses_output_outside_root(self):
        req = self.request(self.manifest(frames=[]), output_path=str(self.outside / "o.json"))
        with self.assertRaisesRegex(RunnerRefused, "output_path is outside"):
            guard.validate_request(req, self.config, self.root)

    def test_refuses_manifest_outside_root(self):
        manifest = self.outside / "m.json"
        manifest.write_text("{}")
        with self.assertRaisesRegex(RunnerRefused, "manifest_path is outside"):
            guard.validate_request(self.request(manifest), self.config, self.root)

    def test_refuses_input_outside_root(self):
        frame = self.outside / "f.png"
        frame.write_bytes(b"png")
        with self.assertRaisesRegex(RunnerRefused, "Witness source is outside"):
            guard.validate_request(self.request(self.manifest(frames=[{"path": str(frame)}])),
                                   self.config, self.root)

    def test_refuses_input_that_is_not_a_file(self):
        with self.assertRaisesRegex(RunnerRefused, "not a file"):
            guard.validate_request(self.request(self.manifest(audio_path=str(self.root / "nope.wav"))),
                                   self.config, self.root)

    def test_refuses_symlink_escaping_root(self):
        link = self.root / "link.png"
        target = self.outside / "real.png"
        target.write_bytes(b"png")
        link.symlink_to(target)
        with self.assertRaisesRegex(RunnerRefused, "outside"):
            guard.validate_request(self.request(self.manifest(frames=[{"path": str(link)}])),
                                   self.config, self.root)

    def test_refuses_budget_override_and_bad_timeout(self):
        manifest = self.manifest(frames=[])
        with self.assertRaisesRegex(RunnerRefused, "estimated_bytes differs"):
            guard.validate_request(self.request(manifest, estimated_bytes=1), self.config, self.root)
        with self.assertRaisesRegex(RunnerRefused, "timeout"):
            guard.validate_request(self.request(manifest, timeout=99999), self.config, self.root)

    def test_refuses_unconfigured_or_unknown_stage(self):
        manifest = self.manifest(frames=[])
        with self.assertRaisesRegex(RunnerRefused, "not configured"):
            guard.validate_request(self.request(manifest, stage="asr_w1"), self.config, self.root)
        with self.assertRaisesRegex(RunnerRefused, "Unknown"):
            guard.validate_request(self.request(manifest, stage="nope"), self.config, self.root)


class WorkerModelPaths(Base):
    def test_unlisted_model_key_refused(self):
        with self.assertRaisesRegex(RunnerRefused, "not listed"):
            worker.resolve_models("ocr", {"model_params": {"Det.model_path": str(self.model)}},
                                  self.config["stages"]["ocr"])

    def test_manifest_path_differing_from_config_refused(self):
        with self.assertRaisesRegex(RunnerRefused, "not the one listed"):
            worker.resolve_models("vlm", {"model_path": str(self.other)}, self.config["stages"]["vlm"])

    def test_configured_path_accepted_and_manifest_may_omit_it(self):
        want = {"model_path": str(self.model.resolve())}
        self.assertEqual(worker.resolve_models("vlm", {"model_path": str(self.model)},
                                               self.config["stages"]["vlm"]), want)
        self.assertEqual(worker.resolve_models("vlm", {}, self.config["stages"]["vlm"]), want)

    def test_stage_without_listed_model_refused(self):
        with self.assertRaisesRegex(RunnerRefused, "not listed"):
            worker.resolve_models("asr_w2", {}, {"models": {}})

    def test_execute_refuses_unlisted_model_before_loading_anything(self):
        frame = self.root / "f.jpg"
        frame.write_bytes(b"j")
        manifest = self.manifest("vlm-manifest.json", model_path=str(self.other),
                                 batches=[{"id": "b", "frames": [{"path": str(frame), "t": 0.0}]}])
        req = {"stage": "vlm", "config_path": self.config["_path"], "root": str(self.root),
               "manifest_path": str(manifest), "output_path": str(self.root / "o-raw.json")}
        with self.assertRaisesRegex(RunnerRefused, "not the one listed"):
            worker.execute(req, require_lease=False)

    def test_execute_refuses_input_outside_root(self):
        frame = self.outside / "f.png"
        frame.write_bytes(b"p")
        manifest = self.manifest(frames=[{"path": str(frame), "t": 0}])
        req = {"stage": "ocr", "config_path": self.config["_path"], "root": str(self.root),
               "manifest_path": str(manifest), "output_path": str(self.root / "o-raw.json")}
        with self.assertRaisesRegex(RunnerRefused, "outside"):
            worker.execute(req, require_lease=False)

    def test_whisper_flags(self):
        cmd = worker.whisper_command({"binary": "b", "model_path": "m"}, "a.wav", Path("p"), {})
        self.assertIn("-ojf", cmd)
        self.assertNotIn("-nt", cmd)
        with self.assertRaises(RunnerRefused):
            worker.whisper_command({"binary": "b", "model_path": "m"}, "a.wav", Path("p"),
                                   {"extra_flags": ["-nt"]})

    def test_whisper_timing_issues_are_flagged_not_clipped(self):
        raw = {"transcription": [{"text": "a", "offsets": {"from": 0, "to": 0}},
                                 {"text": "b", "offsets": {"from": 500, "to": 99000}}]}
        rows, issues = worker.whisper_segments(raw, 10.0)
        self.assertEqual(rows[1]["t_end"], 99.0)
        self.assertEqual([i["issues"] for i in issues],
                         [["zero_or_negative_duration"], ["outside_source_audio"]])


class NetworkDenial(unittest.TestCase):
    def test_socket_events_raise(self):
        for event in ("socket.connect", "socket.connect_ex", "socket.getaddrinfo"):
            with self.assertRaisesRegex(RuntimeError, "cannot access network"):
                worker.deny_network(event, ())
        worker.deny_network("open", ())  # unrelated events pass


class FakeLease:
    lease_id = "L1"
    reserved_bytes = 1

    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class FakeBroker:
    def __init__(self, error=None):
        self.error, self.lease = error, FakeLease()

    def reserve(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.lease


def child(code):
    def spawn():
        return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    return spawn


class GuardFlow(unittest.TestCase):
    REQUEST = {"stage": "ocr", "estimated_bytes": 1 << 30, "timeout": 30, "parent_pid": os.getpid()}

    def run_flow(self, broker, spawn, probe=lambda: {"pressure": "normal"}, **kw):
        return guard.run_guard(dict(self.REQUEST), broker=broker, preflight=lambda *a, **k: {"checks": []},
                               probe=probe, spawn=spawn, verify_lease=lambda lease: None,
                               stop=threading.Event(), sleep=lambda s: None, **kw)

    def test_refused_reserve_spawns_nothing(self):
        broker = FakeBroker(RuntimeError("machine memory refused"))
        spawned = []
        report, retained, worker_ = self.run_flow(broker, lambda: spawned.append(1))
        self.assertEqual(report["status"], "refused")
        self.assertIn("machine memory refused", report["error"])
        self.assertEqual((spawned, retained, worker_), ([], None, None))
        self.assertEqual(broker.lease.released, 0)
        self.assertFalse(report["lease_released"])

    def test_refused_preflight_takes_no_lease(self):
        broker = FakeBroker()

        def refuse(*a, **k):
            raise RuntimeError("daemon holds a model")
        report, *_ = guard.run_guard(dict(self.REQUEST), broker=broker, preflight=refuse,
                                     probe=lambda: {}, spawn=child("pass"), verify_lease=lambda lease: None,
                                     stop=threading.Event())
        self.assertEqual(report["status"], "refused")
        self.assertNotIn("lease_id", report)

    def test_worker_nonzero_exit_is_failed_and_lease_released(self):
        broker = FakeBroker()
        code = "import sys; sys.stderr.write('boom'); print('{\"status\": \"failed\", \"error\": \"x\"}'); sys.exit(3)"
        report, retained, _ = self.run_flow(broker, child(code))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["worker_exit_code"], 3)
        self.assertIn("boom", report["worker_stderr_tail"])
        self.assertTrue(report["worker_stopped"] and report["lease_released"])
        self.assertEqual((broker.lease.released, retained), (1, None))
        for key in ("reserved_bytes", "initial_safety", "safety_samples"):
            self.assertIn(key, report)

    def test_successful_worker_reports_ok(self):
        broker = FakeBroker()
        code = "import sys; sys.stdin.read(); print('{\"status\": \"ok\", \"sha256\": \"ab\"}')"
        report, *_ = self.run_flow(broker, child(code))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["result"]["sha256"], "ab")
        self.assertTrue(report["lease_released"])

    def test_unsafe_memory_stops_worker_and_releases(self):
        broker = FakeBroker()
        calls = []

        def probe():
            calls.append(1)
            if len(calls) > 2:
                raise guard.GuardRefused("Memory pressure is critical")
            return {"pressure": "normal"}
        report, retained, _ = self.run_flow(broker, child("import time; time.sleep(60)"), probe=probe)
        self.assertEqual(report["status"], "failed")
        self.assertIn("critical", report["error"])
        self.assertTrue(report["worker_stopped"] and report["lease_released"])
        self.assertIsNone(retained)

    def test_unconfirmed_worker_death_retains_lease(self):
        broker = FakeBroker()
        report, retained, _ = self.run_flow(broker, child("pass"), cleanup=lambda w: False)
        self.assertEqual(report["status"], "refused")
        self.assertIs(retained, broker.lease)
        self.assertEqual(broker.lease.released, 0)


if __name__ == "__main__":
    unittest.main()
