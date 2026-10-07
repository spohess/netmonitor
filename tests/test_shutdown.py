import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHILD = '''
import asyncio
import sys
from pathlib import Path
import collector

settings = collector.load_config(sys.argv[1])

class ICMP:
    def __init__(self, config):
        self.config = config
    async def prepare(self):
        Path(sys.argv[2]).touch()
    async def collect(self):
        return [{"target_id": target["id"], "status": "success", "rtt_ms": 1, "error": None} for target in self.config["targets"]]

class DNS:
    def __init__(self, *args):
        pass
    async def collect(self, resolver):
        await asyncio.sleep(30)

collector.ICMPProbe = ICMP
collector.DNSProbe = DNS
sys.exit(asyncio.run(collector.execute(settings, False)))
'''


class ShutdownTests(unittest.TestCase):
    def test_sigterm_closes_run_and_flushes_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            settings = json.loads((ROOT / "config.json").read_text())
            settings["database_path"] = str(temp / "monitor.db")
            settings["flush_interval_seconds"] = 0.01
            path = temp / "config.json"
            path.write_text(json.dumps(settings))
            ready = temp / "ready"
            child = subprocess.Popen([sys.executable, "-c", CHILD, str(path), str(ready)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                observed = False
                while time.monotonic() < deadline and child.poll() is None:
                    if ready.exists():
                        with sqlite3.connect(temp / "monitor.db") as connection:
                            observed = connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0] == 3
                        if observed:
                            break
                    time.sleep(0.01)
                self.assertTrue(observed)
                child.terminate()
                stdout, stderr = child.communicate(timeout=3)
                self.assertEqual(child.returncode, 0, stdout + stderr)
                with sqlite3.connect(temp / "monitor.db") as connection:
                    ended, reason = connection.execute("SELECT ended_us,reason FROM collector_runs").fetchone()
                    self.assertIsNotNone(ended)
                    self.assertEqual(reason, "clean_stop")
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0], 3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
