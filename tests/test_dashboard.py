import copy
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import load_config
from dashboard import APIError, Dashboard, DashboardServer, load_dashboard_config
from database import Database, History
from test_netmonitor import icmp_event
from test_speedtest import start_event, end_event


ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = load_config(ROOT / "config.json")
        self.settings["database_path"] = str(Path(self.temp.name) / "monitor.db")
        self.db = Database(self.settings["database_path"])
        self.run_id = self.db.start_run(self.settings, 1_000_000)
        self.now = 12_000_000
        self.dashboard = Dashboard(self.settings, clock=lambda: self.now)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def write(self, seconds, statuses=None):
        self.db.write_events([icmp_event(self.settings, seconds, statuses)], self.run_id, self.settings)

    def test_status_stale_future_and_missing_resolver(self):
        self.write(10)
        result = self.dashboard.status()
        self.assertFalse(result["latest_icmp"]["stale"])
        self.assertEqual(result["latest_icmp"]["current_diagnosis"], "icmp_normal")
        self.assertIsNone(result["latest_dns"]["system"])
        self.now = 17_000_000
        self.assertEqual(self.dashboard.status()["latest_icmp"]["current_diagnosis"], "unknown")
        self.now = 7_000_000
        item = self.dashboard.status()["latest_icmp"]
        self.assertTrue(item["clock_anomaly"])
        self.assertTrue(item["stale"])
        self.assertEqual(item["current_diagnosis"], "unknown")

    def test_status_uses_run_interval_and_independent_dns_freshness(self):
        self.settings["icmp_interval_seconds"] = 10
        self.db.connection.execute("UPDATE collector_runs SET interval_seconds=10")
        self.write(1)
        self.db.write_events([{"kind":"dns", "id":"dns1", "resolver_id":"system", "path":"system", "name":"example.com", "record_type":"A", "timestamp_us":1_000_000, "mono":1, "duration_ms":1, "status":"success", "answers":["192.0.2.1"]}], self.run_id, self.settings)
        self.now = 40_000_000
        result = self.dashboard.status()
        self.assertTrue(result["latest_icmp"]["stale"])
        self.assertFalse(result["latest_dns"]["system"]["stale"])

    def test_empty_and_missing_database_are_distinct_and_never_created(self):
        result = self.dashboard.status()
        self.assertIsNone(result["latest_icmp"])
        metrics = self.dashboard.history("1h")
        self.assertTrue(all(t["loss"] is None for t in metrics["targets"].values()))
        missing = copy.deepcopy(self.settings)
        missing["database_path"] = str(Path(self.temp.name) / "missing.db")
        with self.assertRaises(APIError) as error:
            Dashboard(missing).api("/api/status", {})
        self.assertEqual(error.exception.code, "database_missing")
        self.assertFalse(Path(missing["database_path"]).exists())

    def test_history_matches_existing_metrics_and_excludes_operational_errors(self):
        self.now = 4_000_000_000
        self.write(3990)
        self.write(3992, ["loss"] * 3)
        self.write(3994, ["operational_error"] * 3)
        actual = self.dashboard.history("1h")
        with History(self.settings["database_path"]) as history:
            expected = history.window("1h", self.now)
        self.assertEqual(actual["targets"], expected["targets"])
        self.assertEqual(actual["targets"]["gateway"]["loss"], .5)
        self.assertEqual(actual["targets"]["gateway"]["operational_errors"], 1)
        self.assertNotIn("latest_icmp", actual)

    def test_incident_remains_open_and_pagination_preserves_window(self):
        for second in (2,4,6):
            self.write(second, ["loss"] * 3)
        args = {"start_us":["1000000"],"end_us":["12000000"],"limit":["1"]}
        first = self.dashboard.page("incidents",args)
        self.assertTrue(first["items"][0]["open"])
        self.assertIsNone(first["items"][0]["ended_us"])
        seen = [first["items"][0]["id"]]
        while first["next_cursor"] is not None:
            args["cursor"] = [str(first["next_cursor"])]
            first = self.dashboard.page("incidents",args)
            seen.extend(item["id"] for item in first["items"])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), self.db.connection.execute("SELECT count(*) FROM incidents").fetchone()[0])

    def test_latest_failed_speedtest_does_not_hide_last_success_or_become_zero(self):
        self.db.write_events([start_event("old",1_000_000),end_event("old",3_000_000), start_event("new",10_000_000), {"kind":"speedtest_end","id":"new","timestamp_us":11_000_000,"duration_seconds":1,"result":{"status":"failed","error":"fixture"}}], self.run_id, self.settings)
        self.now = 8_000_000_000
        status = self.dashboard.status()
        self.assertEqual(status["latest_speedtest"]["id"],"new")
        self.assertIsNone(status["latest_speedtest"]["download_mbps"])
        self.assertEqual(status["latest_successful_speedtest"]["id"],"old")
        self.assertEqual(self.dashboard.history("1h")["speedtests"]["samples"],0)

    def test_read_snapshot_does_not_block_writer_or_allow_writes(self):
        self.write(2)
        with self.dashboard.open_history() as history:
            before = history.connection.execute("SELECT count(*) FROM rounds").fetchone()[0]
            self.write(4)
            self.assertEqual(history.connection.execute("SELECT count(*) FROM rounds").fetchone()[0],before)
            with self.assertRaises(sqlite3.OperationalError):
                history.connection.execute("DELETE FROM rounds")
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM rounds").fetchone()[0],2)

    def test_gaps_pagination_and_cache_expiration(self):
        with self.db.connection:
            for second in (2,4,6):
                self.db.add_gap(self.run_id,second*1_000_000,"icmp_late",.1)
        args = {"start_us":["1000000"],"end_us":["12000000"],"limit":["2"]}
        first = self.dashboard.page("gaps", args)
        args["cursor"] = [str(first["next_cursor"])]
        second = self.dashboard.page("gaps",args)
        self.assertEqual(len(first["items"])+len(second["items"]),3)
        self.assertIsNone(second["next_cursor"])
        with patch("dashboard.time.monotonic",return_value=1):
            initial = self.dashboard.history("1h")
        self.write(10)
        with patch("dashboard.time.monotonic",return_value=20):
            self.assertIs(self.dashboard.history("1h"),initial)
        with patch("dashboard.time.monotonic",return_value=32):
            self.assertEqual(self.dashboard.history("1h")["targets"]["gateway"]["samples"],1)

    def test_invalid_parameters_and_schema_rejected(self):
        for path,args in [("/api/history",{"window":["30d"]}), ("/api/history",{"window":["1h","7d"]}), ("/api/gaps",{"start_us":["0"],"end_us":["999999999999999"]}), ("/api/status",{"extra":["1"]})]:
            with self.subTest(path=path,args=args), self.assertRaises(APIError):
                self.dashboard.api(path,args)
        with self.db.connection:
            self.db.connection.execute("DELETE FROM schema_version WHERE version=3")
        with self.assertRaises(APIError) as error:
            self.dashboard.api("/api/status",{})
        self.assertEqual(error.exception.code,"schema_unsupported")

    def test_http_assets_errors_and_host_validation(self):
        server = DashboardServer(("127.0.0.1",0),self.dashboard)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            connection.request("GET","/")
            response = connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertIn("Content-Security-Policy",response.headers)
            self.assertIn(b"app.js",response.read())
            connection.request("GET","/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertIsNone(json.loads(response.read())["latest_icmp"])
            connection.request("GET","/api/history?window=30d")
            response = connection.getresponse()
            self.assertEqual(response.status,400)
            response.read()
            connection.request("GET","/../MEMORY.md")
            response = connection.getresponse()
            self.assertEqual(response.status,404)
            response.read()
            connection.request("GET","/api/status",headers={"Host":"external.example"})
            response = connection.getresponse()
            self.assertEqual(response.status,403)
            response.read()
            connection.request("GET","/api/status",headers={"Origin":"http://external.example"})
            response = connection.getresponse()
            self.assertEqual(response.status,403)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_config_relative_paths_and_loopback_only(self):
        path = Path(self.temp.name)/"dashboard.json"
        raw = {"host":"127.0.0.1","port":8787,"timezone":"America/Sao_Paulo","collector_config":"collector.json"}
        path.write_text(json.dumps(raw))
        self.assertEqual(load_dashboard_config(path)["collector_config"],str(path.parent/"collector.json"))
        raw["host"] = "0.0.0.0"
        path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            load_dashboard_config(path)


if __name__ == "__main__":
    unittest.main()
