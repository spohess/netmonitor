import asyncio
import copy
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import collector
import database
from collector import Clock, Collector, ConfigError, DNSProbe, ICMPProbe, ProcessLock, ProcessRunner, advance_deadline, diagnose, load_config, parse_fping, read_link
from database import Database, History


ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(ROOT / "config.json")


def icmp_event(settings, seconds, statuses=None, rtts=None, timestamp=None):
    statuses = statuses or ["success"] * 3
    rtts = rtts or [1, 10, 12]
    samples = [{"target_id": target["id"], "status": status, "rtt_ms": rtt if status == "success" else None, "error": "fixture" if status == "operational_error" else None} for target, status, rtt in zip(settings["targets"], statuses, rtts)]
    diagnosis, scopes = diagnose(settings["targets"], samples)
    return {"kind": "icmp", "id": "r" + str(seconds), "timestamp_us": int(seconds * 1_000_000) if timestamp is None else timestamp, "mono": seconds, "samples": samples, "diagnosis": diagnosis, "scopes": scopes, "link": {"carrier": None}}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.raw = json.loads((ROOT / "config.json").read_text())

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.raw))
        return load_config(self.path)

    def test_paths_are_config_relative_and_cwd_independent(self):
        loaded = self.load()
        self.assertEqual(loaded["database_path"], str((Path(self.temp.name) / "data/monitor.db").resolve()))
        with patch("pathlib.Path.cwd", return_value=Path("/")):
            self.assertEqual(load_config("config.json")["database_path"], str(ROOT / "data/monitor.db"))

    def test_invalid_values_fail_early(self):
        for key, value in (("icmp_interval_seconds", 0), ("icmp_timeout_ms", True), ("max_queue_size", 0), ("failure_threshold", 1.5), ("retention_days", "30"), ("process_timeout_seconds", 0.1), ("log_level", "NOPE"), ("flush_interval_seconds", 3)):
            with self.subTest(key=key):
                original = self.raw[key]
                self.raw[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.raw[key] = original

    def test_invalid_json(self):
        self.path.write_text("{")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_duplicate_target_and_non_numeric_address(self):
        self.raw["targets"][1]["id"] = "gateway"
        with self.assertRaises(ConfigError):
            self.load()
        self.raw["targets"][1]["id"] = "other"
        self.raw["targets"][1]["address"] = "example.com"
        with self.assertRaises(ConfigError):
            self.load()

    def test_dns_validation_and_retention_warning(self):
        self.raw["dns"]["timeout_seconds"] = 30
        with self.assertRaises(ConfigError):
            self.load()
        self.raw["dns"]["timeout_seconds"] = 3
        self.raw["retention_days"] = 3
        with self.assertLogs("netmonitor", level="WARNING"):
            self.load()


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.targets = config()["targets"]

    def test_stdout_stderr_decimal_and_nonzero_loss(self):
        parsed = parse_fping(self.targets, 1, "10.10.10.254 : 0.73\n", "1.1.1.1 : -\n8.8.8.8 : 11.68\n")
        self.assertEqual([s["status"] for s in parsed], ["success", "loss", "success"])
        self.assertEqual(parsed[0]["rtt_ms"], 0.73)
        self.assertIsNone(parsed[1]["rtt_ms"])

    def test_partial_output_does_not_invent_loss(self):
        parsed = parse_fping(self.targets, 1, "10.10.10.254 : 0.7", "")
        self.assertEqual([s["status"] for s in parsed], ["success", "operational_error", "operational_error"])

    def test_operational_return_codes_and_invalid_output(self):
        for code, output in ((2, "not found"), (3, "invalid argument"), (4, "permission denied"), (0, "strange output"), (0, "10.10.10.254 : nan"), (0, "10.10.10.254 : -1"), (0, "10.10.10.254 : 1 2")):
            with self.subTest(code=code, output=output):
                parsed = parse_fping(self.targets, code, "", output)
                self.assertEqual(parsed[0]["status"], "operational_error")
                self.assertFalse(any(s["status"] == "loss" for s in parsed))

    def test_zero_rtt_is_valid_and_duplicate_is_unknown(self):
        self.assertEqual(parse_fping(self.targets, 0, "10.10.10.254 : 0", "")[0]["rtt_ms"], 0)
        self.assertEqual(parse_fping(self.targets, 0, "10.10.10.254 : 1\n10.10.10.254 : 2", "")[0]["status"], "operational_error")


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_binary_and_permission_denied(self):
        for error in (FileNotFoundError("missing"), PermissionError("denied")):
            result = await ProcessRunner(AsyncMock(side_effect=error)).run(["fake"], 0.1)
            self.assertIn("spawn_error", result[3])

    async def test_timeout_kills_and_reaps_real_local_child(self):
        spawned = []
        async def spawn(*args, **kwargs):
            process = await asyncio.create_subprocess_exec(*args, **kwargs)
            spawned.append(process)
            return process
        result = await ProcessRunner(spawn).run([sys.executable, "-c", "import time; time.sleep(30)"], 0.05)
        self.assertEqual(result[3], "process_timeout")
        self.assertIsNotNone(spawned[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawned[0].pid, 0)

    async def test_cancel_reaps_process(self):
        ready = asyncio.Event()
        processes = []
        async def spawn(*args, **kwargs):
            process = await asyncio.create_subprocess_exec(*args, **kwargs)
            processes.append(process)
            ready.set()
            return process
        task = asyncio.create_task(ProcessRunner(spawn).run([sys.executable, "-c", "import time; time.sleep(30)"], 20))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(processes[0].returncode)

    async def test_group_permission_error_falls_back_to_child_pid(self):
        process = SimpleNamespace(pid=1234, send_signal=Mock())
        with patch("collector.os.killpg", side_effect=PermissionError("group denied")):
            ProcessRunner()._signal(process, signal.SIGTERM)
        process.send_signal.assert_called_once_with(signal.SIGTERM)

    async def test_incremental_stdout_is_not_truncated(self):
        result = await ProcessRunner().run([sys.executable, "-c", "import sys,time; print('first',flush=True); time.sleep(.02); print('second')"], 1)
        self.assertIsNone(result[3])
        self.assertEqual(result[1], "first\nsecond\n")

    async def test_output_is_bounded(self):
        result = await ProcessRunner().run([sys.executable, "-c", "print('x'*200000)"], 1)
        self.assertIn("64 KiB", result[3])

    async def test_fping_options_and_shell_free_arguments(self):
        settings = config()
        runner = SimpleNamespace(run=AsyncMock(side_effect=[(0, "-C -q -t -p -r", "", None), (1, "", "10.10.10.254 : -\n1.1.1.1 : -\n8.8.8.8 : -", None)]))
        probe = ICMPProbe(settings, runner)
        self.assertIsNone(await probe.prepare())
        self.assertEqual([s["status"] for s in await probe.collect()], ["loss"] * 3)
        self.assertIsInstance(runner.run.call_args.args[0], list)
        self.assertIn("-C", runner.run.call_args.args[0])

    async def test_missing_options_produce_operational_samples(self):
        probe = ICMPProbe(config(), SimpleNamespace(run=AsyncMock(return_value=(0, "old version", "", None))))
        self.assertIsNotNone(await probe.prepare())
        self.assertEqual({s["status"] for s in await probe.collect()}, {"operational_error"})


class DiagnosisTests(unittest.TestCase):
    def test_decision_table(self):
        settings = config()
        fixtures = [(["success"] * 3, "icmp_normal", True, True), (["success", "loss", "success"], "target_or_path_degraded", True, True), (["success", "loss", "loss"], "wan_unavailable_or_icmp_filtered", False, True), (["loss"] * 3, "local_link_or_gateway_wan_unknown", None, False), (["loss", "success", "loss"], "gateway_no_icmp_external_reachable", True, True), (["operational_error"] * 3, "unknown", None, None)]
        for statuses, expected, wan, local in fixtures:
            event = icmp_event(settings, 0, statuses)
            self.assertEqual(event["diagnosis"], expected)
            self.assertIs(event["scopes"]["wan"], wan)
            self.assertIs(event["scopes"]["local"], local)

    def test_link_is_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(read_link("eth0", Path(temp))["carrier"])
            directory = Path(temp) / "eth0"
            directory.mkdir()
            (directory / "carrier").write_text("1\n")
            (directory / "operstate").write_text("up\n")
            self.assertEqual(read_link("eth0", Path(temp))["carrier"], True)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data/monitor.db"
        self.config = config()
        self.db = Database(self.path)
        self.run = self.db.start_run(self.config, 0)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def write(self, seconds, statuses=None, rtts=None, timestamp=None):
        event = icmp_event(self.config, seconds, statuses, rtts, timestamp)
        self.db.write_events([event], self.run, self.config)
        return event

    def test_schema_indices_wal_and_duplicate_rollback(self):
        self.assertEqual(self.db.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 3)
        indices = {r[0] for r in self.db.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"one_open_incident", "probe_target_time", "dns_resolver_time", "incident_scope_time"} <= indices)
        self.assertEqual(self.db.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        event = self.write(2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.write_events([icmp_event(self.config, 4), event], self.run, self.config)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)

    def test_migration_rolls_back_and_preserves_data(self):
        self.write(0)
        with patch.dict(database.MIGRATIONS, {4: ("CREATE TABLE migration_temporary(value TEXT)", "INVALID SQL")}):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.migrate()
        self.assertIsNone(self.db.connection.execute("SELECT name FROM sqlite_master WHERE name='migration_temporary'").fetchone())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0], 3)

    def test_upgrade_from_version_one(self):
        self.db.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE incident_segments")
            connection.execute("DROP INDEX round_speedtest")
            connection.execute("ALTER TABLE rounds DROP COLUMN speedtest_id")
            connection.execute("ALTER TABLE dns_samples DROP COLUMN speedtest_id")
            connection.execute("DROP TABLE speedtest_samples")
            connection.execute("DELETE FROM schema_version WHERE version>=2")
        self.db = Database(self.path)
        self.assertEqual(self.db.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 3)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0], 1)

    def test_confirmation_recovery_and_no_invented_end(self):
        for second in (0, 2):
            self.write(second, ["success", "loss", "loss"])
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.write(4, ["success", "loss", "loss"])
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='wan'").fetchone()
        self.assertEqual((row["started_us"], row["confirmed_us"], row["ended_us"], row["observed_seconds"]), (0, 4_000_000, None, 4))
        self.write(6)
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='wan'").fetchone()
        self.assertEqual(row["recovery_us"], 6_000_000)
        self.assertIsNone(row["ended_us"])
        self.write(8)
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='wan'").fetchone()
        self.assertEqual(row["ended_us"], 8_000_000)
        self.assertEqual(row["observed_seconds"], 6)

    def test_restart_unknown_and_observation_gap(self):
        for second in (0, 2, 4):
            self.write(second, ["loss"] * 3)
        self.db.end_run(self.run, 5_000_000, "clean_stop")
        self.run = self.db.start_run(self.config, 100_000_000)
        self.write(100, ["operational_error"] * 3)
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='local'").fetchone()
        self.assertEqual(row["observed_seconds"], 4)
        self.assertIsNone(row["ended_us"])
        self.assertEqual(row["quality"], "gapped")
        self.write(102)
        self.write(104)
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='local'").fetchone()
        self.assertEqual(row["observed_seconds"], 4)
        self.assertEqual(row["ended_us"], 104_000_000)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM gaps WHERE kind='restart'").fetchone()[0], 1)

    def test_scheduled_gap_breaks_confirmation(self):
        self.write(0, ["loss"] * 3)
        self.write(2, ["loss"] * 3)
        self.write(10, ["loss"] * 3)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)

    def test_metrics_exclude_operational_errors_and_jitter_breaks(self):
        self.write(0, rtts=[1, 10, 12])
        self.write(2, rtts=[3, 15, 14])
        self.write(4, ["loss"] * 3)
        self.write(6, rtts=[8, 20, 20])
        self.write(8, ["operational_error"] * 3)
        self.write(10, rtts=[20, 30, 30])
        self.write(14, rtts=[100, 100, 100])
        with History(self.path) as history:
            result = history.query(0, 14_000_000)
            target = result["targets"]["gateway"]
            self.assertEqual(target["samples"], 6)
            self.assertEqual(target["sent"], 5)
            self.assertEqual(target["loss"], 1 / 5)
            self.assertEqual(target["rtt_max_ms"], 20)
            self.assertEqual(target["jitter_ms"], 2)
            self.assertEqual(target["jitter_pairs"], 1)
            self.assertEqual(target["coverage"], 5 / 7)
            self.assertEqual(sum(b["received"] for b in target["buckets"]), 4)

    def test_history_paginates_gaps_and_incidents(self):
        for second in (0, 2, 4):
            self.write(second, ["loss"] * 3)
        with self.db.connection:
            for index in range(1002):
                self.db.add_gap(self.run, 5_000_000, "fixture")
        with History(self.path) as history:
            result = history.query(0, 10_000_000)
            self.assertEqual(result["gaps_total"], 1002)
            self.assertEqual(len(result["gaps"]), 1000)
            self.assertEqual(len(history.gaps(0, 10_000_000, after_id=result["gaps_next_id"])), 2)
            first = history.incidents(0, 10_000_000, limit=1)
            following = history.incidents(0, 10_000_000, limit=1, after_id=first[0]["id"])
            self.assertNotEqual(first[0]["id"], following[0]["id"])

    def test_empty_history_is_unknown(self):
        with History(self.path) as history:
            for window in ("1h", "24h", "7d"):
                target = history.window(window, 1_000_000)["targets"]["gateway"]
                self.assertIsNone(target["loss"])
                self.assertIsNone(target["jitter_ms"])
                self.assertIsNone(target["availability"])
                self.assertEqual(target["coverage"], 0)

    def test_incident_overlap_and_window_clipping(self):
        for second in (0, 2, 4):
            self.write(second, ["loss"] * 3)
        self.write(6)
        self.write(8)
        with History(self.path) as history:
            incident = next(i for i in history.query(3_000_000, 7_000_000)["incidents"] if i["scope"] == "local")
            self.assertEqual(incident["civil_span_seconds"], 4)
            self.assertEqual(incident["observed_seconds_in_window"], 3)
            self.assertEqual(history.incidents(8_000_000, 10_000_000), [])

    def test_wall_clock_rollback_does_not_create_negative_duration(self):
        self.write(0, ["loss"] * 3, timestamp=10_000_000)
        self.write(2, ["loss"] * 3, timestamp=8_000_000)
        self.write(4, ["loss"] * 3, timestamp=6_000_000)
        row = self.db.connection.execute("SELECT * FROM incidents WHERE scope='local'").fetchone()
        self.assertEqual(row["observed_seconds"], 4)
        self.assertEqual(row["quality"], "gapped")

    def test_retention_preserves_open_incidents_and_reports_removal(self):
        for second in (0, 2, 4):
            self.write(second, ["loss"] * 3)
        self.config["retention_batch_size"] = 1
        self.assertLessEqual(self.db.retain_batch(40 * 86400 * 1_000_000, self.config), 3)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 2)
        self.assertGreater(self.db.connection.execute("SELECT COUNT(*) FROM incidents WHERE ended_us IS NULL").fetchone()[0], 0)
        with History(self.path) as history:
            self.assertTrue(history.query(0, 10_000_000)["coverage"]["data_removed"])

    def test_second_process_cannot_acquire_lock_or_write(self):
        script = "import sys; from collector import ProcessLock; from database import Database; lock=ProcessLock(sys.argv[1]); lock.__enter__(); Database(sys.argv[1])"
        with ProcessLock(self.path):
            child = subprocess.run([sys.executable, "-c", script, str(self.path)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("Outra instância", child.stderr)
        with ProcessLock(self.path):
            pass

    def test_target_id_cannot_silently_change_history(self):
        altered = copy.deepcopy(self.config)
        altered["targets"][0]["address"] = "10.10.10.253"
        with self.assertRaises(ValueError):
            self.db.start_run(altered, 2_000_000)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0], 1)

    def test_dns_is_separate_and_resolver_metrics_exist(self):
        self.write(0)
        event = {"kind": "dns", "id": "dns1", "resolver_id": "system", "path": "127.0.0.53", "name": "example.com", "record_type": "A", "timestamp_us": 0, "mono": 0, "duration_ms": 3000, "status": "timeout", "error": "fixture", "answers": []}
        self.db.write_events([event], self.run, self.config)
        with History(self.path) as history:
            result = history.query(0, 30_000_000)
            self.assertEqual(result["resolvers"]["system"]["counts"], {"timeout": 1})
            self.assertEqual(result["targets"]["gateway"]["lost"], 0)
            self.assertEqual(len(result["resolvers"]["system"]["buckets"]), 1)


class FakeClock:
    def __init__(self):
        self.now = 0
        self.wall = 100_000_000

    def monotonic(self):
        return self.now

    def utc_us(self):
        self.wall -= 1_000_000
        return self.wall

    async def wait(self, stop, delay):
        self.now += max(0, delay)
        await asyncio.sleep(0)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_monotonic_cadence_overruns_and_no_overlap(self):
        settings = config()
        clock = FakeClock()
        instance = Collector(settings, None, "run", icmp=SimpleNamespace(), dns=SimpleNamespace(), clock=clock)
        starts = []
        active = 0
        async def operation():
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            starts.append(clock.now)
            clock.now += 5 if len(starts) == 2 else 0.5
            await asyncio.sleep(0)
            active -= 1
            if len(starts) == 4:
                instance.stop.set()
        await instance.schedule("icmp", 2, operation)
        self.assertEqual(starts, [0, 2, 8, 10])
        gaps = []
        while not instance.queue.empty():
            gaps.append(instance.queue.get_nowait())
        self.assertEqual(gaps[0]["reason"], "icmp_skipped")
        self.assertEqual(gaps[0]["duration_seconds"], 4)
        self.assertEqual(advance_deadline(0, 10, 2), (12, 5))

    async def test_once_persists_losses_and_flushes(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = config()
            db = Database(Path(temp) / "db")
            run = db.start_run(settings, 0)
            icmp = SimpleNamespace(prepare=AsyncMock(return_value=None), collect=AsyncMock(return_value=icmp_event(settings, 0, ["loss"] * 3)["samples"]))
            dns = SimpleNamespace(collect=AsyncMock(side_effect=lambda r: {"resolver_id": r["id"], "path": r["address"], "name": "example.com", "record_type": "A", "duration_ms": 3, "status": "timeout", "error": "fixture", "answers": []}))
            instance = Collector(settings, db, run, icmp=icmp, dns=dns)
            self.assertEqual(await instance.run(once=True), 0)
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0], 3)
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM dns_samples").fetchone()[0], 2)
            self.assertTrue(instance.queue.empty())
            db.close()

    async def test_dns_does_not_block_icmp_and_stop_flushes(self):
        settings = config()
        settings["icmp_interval_seconds"] = 0.01
        settings["flush_interval_seconds"] = 0.01
        settings["dns"]["interval_seconds"] = 1
        samples = icmp_event(settings, 0)["samples"]
        instance = None
        count = 0
        async def probe():
            nonlocal count
            count += 1
            if count == 4:
                instance.stop.set()
            return samples
        started = asyncio.Event()
        cancelled = []
        async def dns_query(resolver):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(resolver["id"])
                raise
        saved = []
        db = SimpleNamespace(write_events=lambda events, *args: saved.extend(events), retain_batch=lambda *args: 0)
        instance = Collector(settings, db, "run", icmp=SimpleNamespace(prepare=AsyncMock(return_value=None), collect=probe), dns=SimpleNamespace(collect=dns_query))
        await asyncio.wait_for(instance.run(), 1)
        self.assertEqual(count, 4)
        self.assertEqual(len([event for event in saved if event["kind"] == "icmp"]), 4)
        self.assertEqual(len(cancelled), 2)
        self.assertTrue(started.is_set())

    async def test_persistence_failure_stops_collector(self):
        settings = config()
        db = SimpleNamespace(write_events=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("database or disk is full")))
        instance = Collector(settings, db, "run", icmp=SimpleNamespace(prepare=AsyncMock(return_value="missing"), collect=AsyncMock(return_value=icmp_event(settings, 0, ["operational_error"] * 3)["samples"])), dns=SimpleNamespace())
        with self.assertRaises(sqlite3.OperationalError), self.assertLogs(level="ERROR"):
            await instance.run(True)
        self.assertTrue(instance.stop.is_set())

    async def test_queue_limit_is_observable(self):
        settings = config()
        settings["max_queue_size"] = 1
        instance = Collector(settings, None, "run", icmp=SimpleNamespace(), dns=SimpleNamespace())
        await instance.emit({"kind": "gap"})
        with self.assertRaises(RuntimeError), self.assertLogs(level="CRITICAL"):
            await instance.emit({"kind": "icmp"})


class DNSException(Exception):
    def __init__(self, message="fixture", **kwargs):
        super().__init__(message)
        self.kwargs = kwargs


class NXDOMAIN(DNSException):
    pass


class DNSTimeout(DNSException):
    pass


class NoNameservers(DNSException):
    pass


class DNSTests(unittest.IsolatedAsyncioTestCase):
    def fake_probe(self, result=None, exception=None):
        resolver = SimpleNamespace(nameservers=["10.10.10.254"], resolve=AsyncMock(return_value=result, side_effect=exception))
        module = SimpleNamespace(asyncresolver=SimpleNamespace(Resolver=lambda **kwargs: resolver), resolver=SimpleNamespace(NXDOMAIN=NXDOMAIN, NoNameservers=NoNameservers), exception=SimpleNamespace(DNSException=DNSException, Timeout=DNSTimeout), rcode=SimpleNamespace(SERVFAIL=2, REFUSED=5))
        probe = DNSProbe.__new__(DNSProbe)
        probe.dns = module
        probe.config = config()["dns"]
        probe.monotonic = __import__("time").monotonic
        return probe, resolver

    async def test_valid_answer_and_explicit_path(self):
        class Answer(list):
            nameserver = "1.1.1.1"
        probe, resolver = self.fake_probe(Answer(["93.184.216.34"]))
        sample = await probe.collect({"id": "public", "address": "1.1.1.1"})
        self.assertEqual(sample["status"], "success")
        self.assertEqual(sample["path"], "1.1.1.1")
        self.assertEqual(resolver.nameservers, ["1.1.1.1"])
        self.assertEqual(resolver.resolve.call_args.kwargs["lifetime"], 3)

    async def test_nxdomain_servfail_refused_timeout_operational(self):
        for exception, status in ((NXDOMAIN(), "nxdomain"), (DNSTimeout(), "timeout"), (OSError("bad config"), "operational_error"), (NoNameservers(errors=[(None, None, None, None, SimpleNamespace(rcode=lambda: 2))]), "servfail"), (NoNameservers(errors=[(None, None, None, None, SimpleNamespace(rcode=lambda: 5))]), "refused")):
            probe, _ = self.fake_probe(exception=exception)
            self.assertEqual((await probe.collect({"id": "system", "address": "system"}))["status"], status)

    async def test_real_async_timeout_cancels_query(self):
        probe, resolver = self.fake_probe()
        probe.config["timeout_seconds"] = 0.01
        cancelled = []
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        resolver.resolve = slow
        sample = await asyncio.wait_for(probe.collect({"id": "system", "address": "system"}), 0.5)
        self.assertEqual(sample["status"], "timeout")
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
