import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from collector import Collector, ConfigError, load_config, main
from database import Database, History
from speedtest import DEFAULT_SPEEDTEST, SpeedtestProbe, parse_speedtest, validate_speedtest
from test_netmonitor import ROOT, config, icmp_event


RESULT = {
    "type": "result",
    "ping": {"latency": 8.5},
    "download": {"bandwidth": 118750000, "bytes": 1200000000},
    "upload": {"bandwidth": 112500000, "bytes": 1000000000},
    "interface": {"externalIp": "203.0.113.10"},
    "server": {"id": 123, "name": "Fixture", "location": "Local"},
}


def start_event(test_id="speed1", timestamp=3_000_000):
    return {"kind": "speedtest_start", "id": test_id, "timestamp_us": timestamp, "mono": timestamp / 1_000_000, "interface": "eth0", "plan_download_mbps": 1000, "plan_upload_mbps": 1000, "ip_mode": "static", "expected_public_ip": "203.0.113.10"}


def end_event(test_id="speed1", timestamp=5_000_000):
    return {"kind": "speedtest_end", "id": test_id, "timestamp_us": timestamp, "duration_seconds": 2, "result": parse_speedtest(0, json.dumps(RESULT), "")}


class SpeedtestConfigTests(unittest.TestCase):
    def test_old_config_remains_disabled(self):
        raw = json.loads((ROOT / "config.json").read_text())
        raw.pop("speedtest")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.json"
            path.write_text(json.dumps(raw))
            self.assertEqual(load_config(path)["speedtest"], DEFAULT_SPEEDTEST)

    def test_notebook_profile_and_binary_path(self):
        settings = load_config(ROOT / "config.notebook.json")
        self.assertTrue(settings["speedtest"]["enabled"])
        self.assertEqual(settings["speedtest"]["plan_upload_mbps"], 1000)
        parsed = validate_speedtest({"binary": "bin/speedtest"}, ROOT / "config.json")
        self.assertEqual(parsed["binary"], str(ROOT / "bin/speedtest"))

    def test_invalid_config_fails_early(self):
        for key, value in (("enabled", 1), ("interval_seconds", 1), ("timeout_seconds", 0), ("cooldown_seconds", -1), ("plan_download_mbps", True), ("server_id", False), ("ip_mode", "invalid"), ("expected_public_ip", "hostname"), ("binary", "")):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validate_speedtest({key: value}, ROOT / "config.json")

    def test_unknown_fields_and_cli_combination(self):
        with self.assertRaises(ValueError):
            validate_speedtest({"unexpected": 1}, ROOT / "config.json")
        with patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            main(["--speedtest"])
        self.assertEqual(caught.exception.code, 2)


class SpeedtestParserTests(unittest.TestCase):
    def test_bytes_per_second_to_decimal_mbps(self):
        result = parse_speedtest(0, json.dumps(RESULT), "")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["download_mbps"], 950)
        self.assertEqual(result["upload_mbps"], 900)
        self.assertEqual(result["download_bytes"], 1200000000)
        self.assertEqual(result["public_ip"], "203.0.113.10")

    def test_invalid_json_missing_fields_negative_bool_and_nonfinite(self):
        fixtures = ["{", "[]", json.dumps({"type": "result"})]
        for value in (-1, True, float("nan"), float("inf")):
            raw = copy.deepcopy(RESULT)
            raw["download"]["bandwidth"] = value
            fixtures.append(json.dumps(raw))
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                result = parse_speedtest(0, fixture, "")
                self.assertEqual(result["status"], "operational_error")
                self.assertIsNone(result["download_mbps"])

    def test_failure_and_timeout_are_not_zero_bandwidth(self):
        for arguments, status in (((1, "", "No servers available"), "failed"), ((None, "", "", "process_timeout"), "operational_error"), ((None, "", "", "spawn_error: missing"), "operational_error")):
            result = parse_speedtest(*arguments)
            self.assertEqual(result["status"], status)
            self.assertIsNone(result["upload_mbps"])


class SpeedtestProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_cli_and_arguments_without_license_acceptance(self):
        settings = config()
        settings["speedtest"]["server_id"] = 123
        settings["interface"] = "enp2s0"
        runner = SimpleNamespace(run=AsyncMock(side_effect=[(0, "Speedtest by Ookla --format --interface --progress --server-id", "", None), (0, json.dumps(RESULT), "", None)]))
        probe = SpeedtestProbe(settings, runner)
        self.assertIsNone(await probe.prepare())
        self.assertEqual((await probe.collect())["upload_mbps"], 900)
        arguments, timeout = runner.run.call_args.args
        self.assertEqual(timeout, 180)
        self.assertEqual(arguments, ["speedtest", "--format=json", "--progress=no", "--interface=enp2s0", "--server-id=123"])

    async def test_incompatible_binary_is_operational(self):
        runner = SimpleNamespace(run=AsyncMock(return_value=(0, "speedtest-cli --json", "", None)))
        probe = SpeedtestProbe(config(), runner)
        self.assertIsNotNone(await probe.prepare())
        self.assertEqual((await probe.collect())["status"], "operational_error")
        self.assertEqual(runner.run.call_count, 1)


class SpeedtestDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "monitor.db"
        self.settings = config()
        self.db = Database(self.path)
        self.run = self.db.start_run(self.settings, 0)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def write(self, events):
        self.db.write_events(events, self.run, self.settings)

    def test_migration_from_v2_preserves_samples_and_open_incidents(self):
        self.db.close()
        path = Path(self.temp.name) / "old.db"
        with patch.dict(database.MIGRATIONS, {key: value for key, value in database.MIGRATIONS.items() if key <= 2}, clear=True):
            old = Database(path)
            run = "oldrun"
            with old.connection:
                old.connection.execute("INSERT INTO collector_runs VALUES (?,0,NULL,2,NULL)", (run,))
                old.connection.execute("INSERT INTO targets VALUES ('gateway','Gateway','10.10.10.254','gateway')")
                old.connection.execute("INSERT INTO rounds VALUES ('oldround',?,0,0,'icmp_normal','{}')", (run,))
                old.connection.execute("INSERT INTO probe_samples VALUES ('oldround','gateway',0,'success',1,NULL)")
                old.connection.execute("INSERT INTO incidents(scope,started_us,confirmed_us) VALUES ('wan',0,0)")
            old.close()
        self.db = Database(path)
        self.assertEqual(self.db.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 3)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0], 1)
        self.assertIsNone(self.db.connection.execute("SELECT ended_us FROM incidents").fetchone()[0])
        self.assertIsNone(self.db.connection.execute("SELECT speedtest_id FROM rounds").fetchone()[0])

    def test_results_plan_percent_ip_and_window_limits(self):
        self.write([start_event(), end_event()])
        with History(self.path) as history:
            result = history.query(0, 10_000_000)["speedtests"]
            self.assertEqual(result["successful"], 1)
            self.assertEqual(result["transferred_bytes"], 2200000000)
            sample = result["results"][0]
            self.assertEqual(sample["download_plan_percent"], 95)
            self.assertEqual(sample["upload_plan_percent"], 90)
            self.assertTrue(sample["public_ip_matches_expected"])
            self.assertEqual(history.speedtests(0, 3_000_000)["samples"], 0)
            self.assertEqual(history.speedtests(3_000_000, 4_000_000)["samples"], 1)

    def test_latency_baseline_does_not_cross_speedtest(self):
        events = [icmp_event(self.settings, 0, rtts=[10] * 3), icmp_event(self.settings, 2, rtts=[12] * 3), start_event()]
        loaded = icmp_event(self.settings, 4, rtts=[100] * 3)
        loaded["speedtest_id"] = "speed1"
        events += [loaded, end_event(), icmp_event(self.settings, 6, rtts=[15] * 3), icmp_event(self.settings, 8, rtts=[16] * 3)]
        self.write(events)
        with History(self.path) as history:
            metrics = history.query(0, 10_000_000)["targets"]["gateway"]
            self.assertEqual(metrics["rtt_max_ms"], 100)
            self.assertEqual(metrics["speedtest_samples"], 1)
            self.assertEqual(metrics["without_speedtest"]["rtt_max_ms"], 16)
            self.assertEqual(metrics["without_speedtest"]["jitter_ms"], 1.5)
            self.assertEqual(metrics["without_speedtest"]["jitter_pairs"], 2)
            self.assertEqual(sum(bucket["speedtest_samples"] for bucket in metrics["buckets"]), 1)

    def test_crash_marks_interrupted_without_inventing_end(self):
        self.write([start_event()])
        self.db.close()
        self.db = Database(self.path)
        self.db.start_run(self.settings, 100_000_000)
        row = self.db.connection.execute("SELECT * FROM speedtest_samples").fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertIsNone(row["ended_us"])
        self.assertIsNone(row["download_mbps"])

    def test_retention_keeps_running_and_removes_finished(self):
        self.write([start_event(), end_event(), start_event("running", 7_000_000)])
        self.db.retain_batch(40 * 86400 * 1_000_000, self.settings)
        self.assertEqual([r[0] for r in self.db.connection.execute("SELECT id FROM speedtest_samples")], ["running"])

    def test_duplicate_completion_rolls_back(self):
        self.write([start_event(), end_event()])
        with self.assertRaises(ValueError):
            self.write([icmp_event(self.settings, 2), end_event()])
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)


class SpeedtestCollectorTests(unittest.IsolatedAsyncioTestCase):
    def instance(self, speedtest):
        settings = config()
        settings["speedtest"]["cooldown_seconds"] = 0
        icmp = SimpleNamespace(prepare=AsyncMock(return_value=None), collect=AsyncMock(return_value=icmp_event(settings, 0)["samples"]))
        dns = SimpleNamespace(collect=AsyncMock(side_effect=lambda r: {"resolver_id": r["id"], "path": r["address"], "name": "example.com", "record_type": "A", "duration_ms": 1, "status": "success", "error": None, "answers": ["203.0.113.1"]}))
        events = []
        db = SimpleNamespace(write_events=lambda items, *args: events.extend(items), retain_batch=lambda *args: 0)
        return Collector(settings, db, "run", icmp=icmp, dns=dns, speedtest=speedtest), events

    async def test_once_does_not_run_speedtest_unless_explicit(self):
        probe = SimpleNamespace(prepare=AsyncMock(return_value=None), collect=AsyncMock(return_value=parse_speedtest(0, json.dumps(RESULT), "")))
        instance, events = self.instance(probe)
        instance.config["speedtest"]["enabled"] = True
        await instance.run(once=True)
        probe.prepare.assert_not_called()
        instance, events = self.instance(probe)
        await instance.run(once=True, include_speedtest=True)
        self.assertEqual(probe.collect.call_count, 1)
        self.assertEqual([event["kind"] for event in events if event["kind"].startswith("speedtest")], ["speedtest_start", "speedtest_end"])

    async def test_long_speedtest_does_not_block_icmp_and_is_cancelled_on_stop(self):
        ready = asyncio.Event()
        cancelled = []
        async def slow():
            ready.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        instance, events = self.instance(SimpleNamespace(prepare=AsyncMock(return_value=None), collect=slow))
        instance.config["speedtest"].update(enabled=True, initial_delay_seconds=0)
        instance.config["icmp_interval_seconds"] = 0.01
        instance.config["flush_interval_seconds"] = 0.01
        count = 0
        async def icmp():
            nonlocal count
            count += 1
            if count == 5:
                instance.stop.set()
            return icmp_event(instance.config, 0)["samples"]
        instance.icmp.collect = icmp
        await asyncio.wait_for(instance.run(), 2)
        self.assertEqual(count, 5)
        self.assertTrue(cancelled)
        self.assertTrue(any(event.get("speedtest_id") for event in events if event["kind"] == "icmp"))
        self.assertEqual(next(event for event in events if event["kind"] == "speedtest_end")["result"]["status"], "cancelled")
        self.assertIsNone(instance.active_speedtest_id)

    async def test_probe_overlapping_entire_short_speedtest_is_marked(self):
        instance, events = self.instance(SimpleNamespace(collect=AsyncMock(return_value=parse_speedtest(0, json.dumps(RESULT), ""))))
        async def overlap():
            await instance.speedtest_round()
            return icmp_event(instance.config, 0)["samples"]
        instance.icmp.collect = overlap
        await instance.icmp_round()
        queued = []
        while not instance.queue.empty():
            queued.append(instance.queue.get_nowait())
        self.assertIsNotNone(next(item for item in queued if item["kind"] == "icmp")["speedtest_id"])

    async def test_speedtest_errors_do_not_change_icmp_scopes(self):
        instance, events = self.instance(SimpleNamespace(prepare=AsyncMock(return_value="missing"), collect=AsyncMock(return_value=parse_speedtest(None, "", "", "spawn_error"))))
        self.assertEqual(await instance.run(once=True, include_speedtest=True), 2)
        icmp = next(item for item in events if item["kind"] == "icmp")
        self.assertTrue(icmp["scopes"]["wan"])
        self.assertEqual(icmp["diagnosis"], "icmp_normal")


if __name__ == "__main__":
    unittest.main()
