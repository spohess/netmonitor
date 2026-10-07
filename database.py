import json
import logging
import math
import sqlite3
import uuid
from pathlib import Path


MIGRATIONS = {
    1: (
        "CREATE TABLE targets (id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL, role TEXT NOT NULL)",
        "CREATE TABLE collector_runs (id TEXT PRIMARY KEY, started_us INTEGER NOT NULL, ended_us INTEGER, interval_seconds REAL NOT NULL, reason TEXT)",
        "CREATE TABLE gaps (id INTEGER PRIMARY KEY, run_id TEXT REFERENCES collector_runs(id), timestamp_us INTEGER NOT NULL, kind TEXT NOT NULL, duration_seconds REAL, details TEXT)",
        "CREATE TABLE rounds (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES collector_runs(id), timestamp_us INTEGER NOT NULL, mono REAL NOT NULL, diagnosis TEXT NOT NULL, link_json TEXT NOT NULL)",
        "CREATE TABLE probe_samples (round_id TEXT NOT NULL REFERENCES rounds(id) ON DELETE CASCADE, target_id TEXT NOT NULL REFERENCES targets(id), timestamp_us INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('success','loss','operational_error')), rtt_ms REAL, error TEXT, PRIMARY KEY(round_id,target_id), CHECK((status='success' AND rtt_ms IS NOT NULL AND rtt_ms>=0) OR (status!='success' AND rtt_ms IS NULL)))",
        "CREATE TABLE dns_samples (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES collector_runs(id), resolver_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, record_type TEXT NOT NULL, timestamp_us INTEGER NOT NULL, mono REAL NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL, error TEXT, answers_json TEXT NOT NULL)",
        "CREATE TABLE incidents (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, started_us INTEGER NOT NULL, confirmed_us INTEGER NOT NULL, recovery_us INTEGER, ended_us INTEGER, observed_seconds REAL NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'continuous')",
        "CREATE UNIQUE INDEX one_open_incident ON incidents(scope) WHERE ended_us IS NULL",
        "CREATE TABLE state (scope TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE INDEX probe_target_time ON probe_samples(target_id,timestamp_us)",
        "CREATE INDEX dns_resolver_time ON dns_samples(resolver_id,timestamp_us)",
        "CREATE INDEX incident_scope_time ON incidents(scope,started_us,ended_us)",
        "CREATE INDEX round_time ON rounds(timestamp_us)",
        "CREATE INDEX gap_time ON gaps(timestamp_us)",
    ),
    2: (
        "CREATE TABLE incident_segments (id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE, start_us INTEGER NOT NULL, end_us INTEGER NOT NULL, seconds REAL NOT NULL CHECK(seconds>=0), observations INTEGER NOT NULL DEFAULT 1)",
        "CREATE INDEX segment_incident_time ON incident_segments(incident_id,start_us,end_us)",
    ),
    3: (
        "CREATE TABLE speedtest_samples (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES collector_runs(id), timestamp_us INTEGER NOT NULL, mono REAL NOT NULL, ended_us INTEGER, duration_seconds REAL, status TEXT NOT NULL CHECK(status IN ('running','success','failed','operational_error','cancelled','interrupted')), error TEXT, download_mbps REAL, upload_mbps REAL, download_bytes INTEGER, upload_bytes INTEGER, latency_ms REAL, public_ip TEXT, server_json TEXT NOT NULL DEFAULT '{}', interface TEXT NOT NULL, plan_download_mbps REAL NOT NULL, plan_upload_mbps REAL NOT NULL, ip_mode TEXT NOT NULL, expected_public_ip TEXT)",
        "ALTER TABLE rounds ADD COLUMN speedtest_id TEXT REFERENCES speedtest_samples(id) ON DELETE SET NULL",
        "ALTER TABLE dns_samples ADD COLUMN speedtest_id TEXT REFERENCES speedtest_samples(id) ON DELETE SET NULL",
        "CREATE INDEX speedtest_time ON speedtest_samples(timestamp_us)",
        "CREATE INDEX round_speedtest ON rounds(speedtest_id) WHERE speedtest_id IS NOT NULL",
    ),
}


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=2)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=2000")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.migrate()
        except BaseException:
            self.connection.close()
            raise

    def migrate(self):
        with self.connection:
            self.connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        version = self.connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        if version > max(MIGRATIONS):
            raise ValueError("Banco usa schema mais recente que este coletor")
        for number, statements in sorted(MIGRATIONS.items()):
            if number > version:
                with self.connection:
                    self.connection.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        self.connection.execute(statement)
                    self.connection.execute("INSERT INTO schema_version VALUES (?)", (number,))

    def start_run(self, config, timestamp_us):
        run_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute("UPDATE speedtest_samples SET status='interrupted',error='Execução anterior terminou sem resultado; fim desconhecido' WHERE status='running'")
            for target in config["targets"]:
                previous = self.connection.execute("SELECT address,role FROM targets WHERE id=?", (target["id"],)).fetchone()
                if previous and (previous["address"] != target["address"] or previous["role"] != target["role"]):
                    raise ValueError("Mudança de endereço/papel exige novo id de alvo: " + target["id"])
                self.connection.execute("INSERT INTO targets VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (target["id"], target["name"], target["address"], target["role"]))
            previous = self.connection.execute("SELECT id,ended_us FROM collector_runs ORDER BY started_us DESC LIMIT 1").fetchone()
            self.connection.execute("INSERT INTO collector_runs(id,started_us,interval_seconds) VALUES (?,?,?)", (run_id, timestamp_us, config["icmp_interval_seconds"]))
            if previous:
                last = self.connection.execute("SELECT MAX(timestamp_us) FROM rounds WHERE run_id=?", (previous["id"],)).fetchone()[0]
                self.add_gap(run_id, timestamp_us, "restart", None, json.dumps({"previous_last_sample_us": last, "previous_clean_stop_us": previous["ended_us"]}))
            for row in self.connection.execute("SELECT scope,payload FROM state").fetchall():
                state = json.loads(row["payload"])
                state.update(last_mono=None, last_us=None, last_ok=None, fail_count=0, success_count=0, pending_segments=[])
                if state.get("incident_id"):
                    self._incident_gap(state["incident_id"])
                    self.connection.execute("UPDATE incidents SET recovery_us=NULL WHERE id=?", (state["incident_id"],))
                self._save_state(row["scope"], state)
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('created_us',?)", (str(timestamp_us),))
        return run_id

    def end_run(self, run_id, timestamp_us, reason):
        with self.connection:
            self.connection.execute("UPDATE collector_runs SET ended_us=?,reason=? WHERE id=?", (timestamp_us, reason, run_id))

    def add_gap(self, run_id, timestamp_us, kind, duration_seconds=None, details=None):
        self.connection.execute("INSERT INTO gaps(run_id,timestamp_us,kind,duration_seconds,details) VALUES (?,?,?,?,?)", (run_id, timestamp_us, kind, duration_seconds, details))

    def _save_state(self, scope, state):
        self.connection.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload", (scope, json.dumps(state)))

    def _incident_gap(self, incident_id):
        self.connection.execute("UPDATE incidents SET gap_count=gap_count+1,quality='gapped' WHERE id=?", (incident_id,))

    def observe(self, scope, ok, timestamp_us, mono, run_id, config, interval):
        row = self.connection.execute("SELECT payload FROM state WHERE scope=?", (scope,)).fetchone()
        state = json.loads(row[0]) if row else {"incident_id": None, "fail_count": 0, "success_count": 0, "last_mono": None, "pending_segments": []}
        last_mono = state.get("last_mono")
        continuity = last_mono is not None and state.get("run_id") == run_id and 0 <= mono - last_mono <= interval * 1.75
        if not continuity and last_mono is not None:
            if state.get("incident_id"):
                self._incident_gap(state["incident_id"])
                self.connection.execute("UPDATE incidents SET recovery_us=NULL WHERE id=?", (state["incident_id"],))
            state.update(fail_count=0, success_count=0, pending_segments=[])
        if ok is None:
            if state.get("incident_id") and state.get("last_ok") is not None:
                self._incident_gap(state["incident_id"])
            if state.get("incident_id"):
                self.connection.execute("UPDATE incidents SET recovery_us=NULL WHERE id=?", (state["incident_id"],))
            state.update(fail_count=0, success_count=0, last_mono=None, last_us=None, last_ok=None, pending_segments=[], run_id=run_id)
            self._save_state(scope, state)
            return
        segment = None
        if continuity and state.get("last_ok") is False:
            segment = (state["last_us"], timestamp_us, mono - last_mono)
        if ok is False:
            if state["fail_count"] == 0:
                state["first_failure_us"] = timestamp_us
                state["pending_segments"] = []
            state["fail_count"] += 1
            state["success_count"] = 0
            if not state.get("incident_id") and segment and state["fail_count"] > 1:
                state["pending_segments"].append(segment)
            if not state.get("incident_id") and state["fail_count"] >= config["failure_threshold"]:
                cursor = self.connection.execute("INSERT INTO incidents(scope,started_us,confirmed_us) VALUES (?,?,?)", (scope, state["first_failure_us"], timestamp_us))
                state["incident_id"] = cursor.lastrowid
                for pending in state["pending_segments"]:
                    self._add_segment(state["incident_id"], pending)
                state["pending_segments"] = []
                segment = None
                logging.warning("Incidente confirmado: %s", scope)
            if state.get("incident_id"):
                self.connection.execute("UPDATE incidents SET recovery_us=NULL WHERE id=?", (state["incident_id"],))
        else:
            state["fail_count"] = 0
            state["pending_segments"] = []
            state["success_count"] += 1
            if state.get("incident_id") and state["success_count"] == 1:
                self.connection.execute("UPDATE incidents SET recovery_us=? WHERE id=?", (timestamp_us, state["incident_id"]))
        if state.get("incident_id") and segment:
            self._add_segment(state["incident_id"], segment)
        if ok and state.get("incident_id") and state["success_count"] >= config["recovery_threshold"]:
            self.connection.execute("UPDATE incidents SET ended_us=? WHERE id=?", (timestamp_us, state["incident_id"]))
            logging.info("Incidente encerrado: %s", scope)
            state["incident_id"] = None
        state.update(last_mono=mono, last_us=timestamp_us, last_ok=ok, run_id=run_id)
        self._save_state(scope, state)

    def _add_segment(self, incident_id, segment):
        start, end, seconds = segment
        previous = self.connection.execute("SELECT * FROM incident_segments WHERE incident_id=? ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
        merge = previous and previous["end_us"] == start and end >= start and previous["end_us"] >= previous["start_us"] and end - previous["start_us"] <= 60_000_000
        if merge:
            self.connection.execute("UPDATE incident_segments SET end_us=?,seconds=seconds+?,observations=observations+1 WHERE id=?", (end, seconds, previous["id"]))
        else:
            self.connection.execute("INSERT INTO incident_segments(incident_id,start_us,end_us,seconds) VALUES (?,?,?,?)", (incident_id, start, end, seconds))
        self.connection.execute("UPDATE incidents SET observed_seconds=observed_seconds+? WHERE id=?", (seconds, incident_id))
        if end < start:
            self._incident_gap(incident_id)

    def write_events(self, events, run_id, config):
        with self.connection:
            for event in events:
                kind = event["kind"]
                if kind == "icmp":
                    self.connection.execute("INSERT INTO rounds(id,run_id,timestamp_us,mono,diagnosis,link_json,speedtest_id) VALUES (?,?,?,?,?,?,?)", (event["id"], run_id, event["timestamp_us"], event["mono"], event["diagnosis"], json.dumps(event["link"]), event.get("speedtest_id")))
                    for sample in event["samples"]:
                        self.connection.execute("INSERT INTO probe_samples VALUES (?,?,?,?,?,?)", (event["id"], sample["target_id"], event["timestamp_us"], sample["status"], sample["rtt_ms"], sample.get("error")))
                    for scope, ok in event["scopes"].items():
                        self.observe(scope, ok, event["timestamp_us"], event["mono"], run_id, config, config["icmp_interval_seconds"])
                elif kind == "dns":
                    self.connection.execute("INSERT INTO dns_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (event["id"], run_id, event["resolver_id"], event["path"], event["name"], event["record_type"], event["timestamp_us"], event["mono"], event["duration_ms"], event["status"], event.get("error"), json.dumps(event["answers"]), event.get("speedtest_id")))
                    ok = None if event["status"] == "operational_error" else event["status"] in ("success", "nodata")
                    self.observe("dns:" + event["resolver_id"], ok, event["timestamp_us"], event["mono"], run_id, config, config["dns"]["interval_seconds"])
                elif kind == "speedtest_start":
                    self.connection.execute("INSERT INTO speedtest_samples(id,run_id,timestamp_us,mono,status,interface,plan_download_mbps,plan_upload_mbps,ip_mode,expected_public_ip) VALUES (?,?,?,?,'running',?,?,?,?,?)", (event["id"], run_id, event["timestamp_us"], event["mono"], event["interface"], event["plan_download_mbps"], event["plan_upload_mbps"], event["ip_mode"], event["expected_public_ip"]))
                elif kind == "speedtest_end":
                    result = event["result"]
                    cursor = self.connection.execute("UPDATE speedtest_samples SET ended_us=?,duration_seconds=?,status=?,error=?,download_mbps=?,upload_mbps=?,download_bytes=?,upload_bytes=?,latency_ms=?,public_ip=?,server_json=? WHERE id=? AND run_id=? AND status='running'", (event["timestamp_us"], event["duration_seconds"], result["status"], result.get("error"), result.get("download_mbps"), result.get("upload_mbps"), result.get("download_bytes"), result.get("upload_bytes"), result.get("latency_ms"), result.get("public_ip"), json.dumps(result.get("server", {})), event["id"], run_id))
                    if cursor.rowcount != 1:
                        raise ValueError("Speedtest inexistente ou já finalizado")
                elif kind == "gap":
                    self.add_gap(run_id, event["timestamp_us"], event["reason"], event.get("duration_seconds"), event.get("details"))
                else:
                    raise ValueError("Evento desconhecido: " + kind)

    def retain_batch(self, now_us, config):
        cutoff = now_us - int(config["retention_days"] * 86400 * 1_000_000)
        incident_cutoff = now_us - int(config["incident_retention_days"] * 86400 * 1_000_000)
        limit = config["retention_batch_size"]
        deleted = 0
        with self.connection:
            for table in ("rounds", "dns_samples", "gaps"):
                cursor = self.connection.execute("DELETE FROM " + table + " WHERE rowid IN (SELECT rowid FROM " + table + " WHERE timestamp_us<? LIMIT ?)", (cutoff, limit))
                deleted += cursor.rowcount
            self.connection.execute("DELETE FROM incidents WHERE id IN (SELECT id FROM incidents WHERE ended_us IS NOT NULL AND ended_us<? LIMIT ?)", (incident_cutoff, limit))
            cursor = self.connection.execute("DELETE FROM speedtest_samples WHERE id IN (SELECT id FROM speedtest_samples WHERE timestamp_us<? AND status!='running' AND NOT EXISTS(SELECT 1 FROM rounds WHERE speedtest_id=speedtest_samples.id) AND NOT EXISTS(SELECT 1 FROM dns_samples WHERE speedtest_id=speedtest_samples.id) LIMIT ?)", (cutoff, limit))
            deleted += cursor.rowcount
            self.connection.execute("DELETE FROM collector_runs WHERE id IN (SELECT id FROM collector_runs WHERE ended_us<? AND NOT EXISTS(SELECT 1 FROM rounds WHERE run_id=collector_runs.id) AND NOT EXISTS(SELECT 1 FROM dns_samples WHERE run_id=collector_runs.id) AND NOT EXISTS(SELECT 1 FROM gaps WHERE run_id=collector_runs.id) AND NOT EXISTS(SELECT 1 FROM speedtest_samples WHERE run_id=collector_runs.id) LIMIT ?)", (cutoff, limit))
            if deleted:
                self.connection.execute("INSERT INTO metadata VALUES ('retained_since_us',?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))", (str(cutoff),))
        return deleted

    def close(self):
        self.connection.close()


class History:
    def __init__(self, path):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=2)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("PRAGMA busy_timeout=2000")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def window(self, window, end_us, interval_seconds=2, dns_interval_seconds=30, buckets=120):
        durations = {"1h": 3600, "24h": 86400, "7d": 604800}
        if window not in durations:
            raise ValueError("Janela deve ser 1h, 24h ou 7d")
        return self.query(end_us - durations[window] * 1_000_000, end_us, interval_seconds, dns_interval_seconds, buckets)

    def _coverage(self, start_us):
        metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
        return {"history_started_us": int(metadata.get("created_us", 0)), "retained_since_us": int(metadata["retained_since_us"]) if "retained_since_us" in metadata else None, "data_removed": start_us < int(metadata.get("retained_since_us", 0))}

    def query(self, start_us, end_us, interval_seconds=2, dns_interval_seconds=30, buckets=120):
        if end_us <= start_us or interval_seconds <= 0 or dns_interval_seconds <= 0 or not 1 <= buckets <= 1000:
            raise ValueError("Janela, intervalos ou buckets inválidos")
        result = {"start_us": start_us, "end_us": end_us, "timestamp_unit": "UTC Unix microseconds", "coverage": self._coverage(start_us), "targets": {}, "resolvers": {}, "incidents": self.incidents(start_us, end_us)}
        raw_width = max(1, math.ceil((end_us - start_us) / buckets))
        interval_us = int(interval_seconds * 1_000_000)
        width = math.ceil(raw_width / interval_us) * interval_us
        dns_interval_us = int(dns_interval_seconds * 1_000_000)
        dns_width = math.ceil(raw_width / dns_interval_us) * dns_interval_us
        expected = math.ceil((end_us - start_us) / (interval_seconds * 1_000_000))
        for target in self.connection.execute("SELECT * FROM targets"):
            target_id = target["id"]
            stats = self._probe_stats(target_id, start_us, end_us, interval_seconds)
            baseline = dict(stats)
            stats.update(dict(target))
            stats["expected_slots"] = max(expected, stats["samples"])
            stats["coverage"] = stats["sent"] / stats["expected_slots"]
            stats["execution_coverage"] = stats["samples"] / stats["expected_slots"]
            loaded = self.connection.execute("SELECT COUNT(*) FROM probe_samples WHERE target_id=? AND timestamp_us>=? AND timestamp_us<? AND round_id IN (SELECT id FROM rounds WHERE speedtest_id IS NOT NULL)", (target_id, start_us, end_us)).fetchone()[0]
            if loaded:
                baseline = self._probe_stats(target_id, start_us, end_us, interval_seconds, exclude_speedtests=True)
            baseline_expected = max(expected - loaded, baseline["samples"])
            baseline.update(expected_slots=baseline_expected, coverage=baseline["sent"] / baseline_expected if baseline_expected else None)
            stats["speedtest_samples"] = loaded
            stats["without_speedtest"] = baseline
            grouped = self.connection.execute("SELECT CAST((timestamp_us-?)/? AS INTEGER) AS bucket, COUNT(*) AS samples, SUM(status!='operational_error') AS sent, SUM(status='success') AS received, SUM(status='loss') AS lost, SUM(status='operational_error') AS operational_errors, AVG(rtt_ms) AS rtt_mean_ms, MAX(rtt_ms) AS rtt_max_ms, SUM(round_id IN (SELECT id FROM rounds WHERE speedtest_id IS NOT NULL)) AS speedtest_samples FROM probe_samples WHERE target_id=? AND timestamp_us>=? AND timestamp_us<? GROUP BY bucket ORDER BY bucket", (start_us, width, target_id, start_us, end_us)).fetchall()
            stats["buckets"] = self._buckets(grouped, start_us, end_us, width, interval_seconds)
            result["targets"][target_id] = stats
        resolver_ids = self.connection.execute("SELECT DISTINCT resolver_id FROM dns_samples").fetchall()
        for resolver in resolver_ids:
            resolver_id = resolver[0]
            rows = self.connection.execute("SELECT status,COUNT(*) AS count,AVG(duration_ms) AS duration_mean_ms FROM dns_samples WHERE resolver_id=? AND timestamp_us>=? AND timestamp_us<? GROUP BY status", (resolver_id, start_us, end_us)).fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            observed = sum(count for status, count in counts.items() if status != "operational_error")
            sent = sum(counts.values())
            expected_dns = max(sent, math.ceil((end_us - start_us) / (dns_interval_seconds * 1_000_000)))
            dns_buckets = self.connection.execute("SELECT CAST((timestamp_us-?)/? AS INTEGER) AS bucket, COUNT(*) AS samples, SUM(status!='operational_error') AS sent, SUM(status IN ('success','nodata')) AS received, SUM(status NOT IN ('success','nodata','operational_error')) AS lost, SUM(status='operational_error') AS operational_errors, AVG(duration_ms) AS duration_mean_ms, MAX(duration_ms) AS duration_max_ms, SUM(speedtest_id IS NOT NULL) AS speedtest_samples FROM dns_samples WHERE resolver_id=? AND timestamp_us>=? AND timestamp_us<? GROUP BY bucket ORDER BY bucket", (start_us, dns_width, resolver_id, start_us, end_us)).fetchall()
            result["resolvers"][resolver_id] = {"counts": counts, "duration_mean_ms": sum(row["duration_mean_ms"] * row["count"] for row in rows) / sent if sent else None, "expected_slots": expected_dns, "coverage": observed / expected_dns, "query_availability": (counts.get("success", 0) + counts.get("nodata", 0)) / observed if observed else None, "buckets": self._buckets(dns_buckets, start_us, end_us, dns_width, dns_interval_seconds)}
        result["speedtests"] = self.speedtests(start_us, end_us)
        result["gaps"] = self.gaps(start_us, end_us)
        result["gaps_total"] = self.connection.execute("SELECT COUNT(*) FROM gaps WHERE timestamp_us>=? AND timestamp_us<?", (start_us, end_us)).fetchone()[0]
        result["gaps_next_id"] = result["gaps"][-1]["id"] if result["gaps_total"] > len(result["gaps"]) else None
        result["incidents_total"] = self.connection.execute("SELECT COUNT(*) FROM incidents WHERE started_us<? AND (ended_us IS NULL OR ended_us>?)", (end_us, start_us)).fetchone()[0]
        result["incidents_next_id"] = result["incidents"][-1]["id"] if result["incidents_total"] > len(result["incidents"]) else None
        latest = self.connection.execute("SELECT * FROM rounds ORDER BY rowid DESC LIMIT 1").fetchone()
        result["latest_icmp"] = dict(latest) if latest else None
        if latest:
            age = max(0, (end_us - latest["timestamp_us"]) / 1_000_000)
            result["latest_icmp"].update(age_seconds=age, stale=age > interval_seconds * 3, samples=[dict(row) for row in self.connection.execute("SELECT * FROM probe_samples WHERE round_id=?", (latest["id"],))])
            result["latest_icmp"]["link"] = json.loads(result["latest_icmp"].pop("link_json"))
            result["latest_icmp"]["current_diagnosis"] = "unknown" if age > interval_seconds * 3 else latest["diagnosis"]
        result["latest_dns"] = {}
        for resolver in resolver_ids:
            latest_dns = self.connection.execute("SELECT * FROM dns_samples WHERE resolver_id=? ORDER BY rowid DESC LIMIT 1", (resolver[0],)).fetchone()
            item = dict(latest_dns)
            age = max(0, (end_us - item["timestamp_us"]) / 1_000_000)
            item.update(age_seconds=age, stale=age > dns_interval_seconds * 3, current_status="unknown" if age > dns_interval_seconds * 3 else item["status"])
            item["answers"] = json.loads(item.pop("answers_json"))
            result["latest_dns"][resolver[0]] = item
        return result

    def _buckets(self, rows, start_us, end_us, width, interval):
        lookup = {row["bucket"]: dict(row) for row in rows}
        result = []
        for index in range(math.ceil((end_us - start_us) / width)):
            start = start_us + index * width
            end = min(end_us, start + width)
            bucket = lookup.get(index, {"bucket": index, "samples": 0, "sent": 0, "received": 0, "lost": 0, "operational_errors": 0, "speedtest_samples": 0})
            expected = max(bucket["samples"], math.ceil((end - start) / (interval * 1_000_000)))
            bucket.update(start_us=start, end_us=end, expected_slots=expected, coverage=bucket["sent"] / expected, loss=bucket["lost"] / bucket["sent"] if bucket["sent"] else None)
            result.append(bucket)
        return result

    def _probe_stats(self, target_id, start_us, end_us, interval, exclude_speedtests=False):
        selection = " AND NOT EXISTS(SELECT 1 FROM rounds WHERE id=probe_samples.round_id AND speedtest_id IS NOT NULL)" if exclude_speedtests else ""
        row = self.connection.execute("SELECT COUNT(*) AS samples, COALESCE(SUM(status!='operational_error'),0) AS sent, COALESCE(SUM(status='success'),0) AS received, COALESCE(SUM(status='loss'),0) AS lost, COALESCE(SUM(status='operational_error'),0) AS operational_errors, AVG(rtt_ms) AS rtt_mean_ms, MAX(rtt_ms) AS rtt_max_ms FROM probe_samples WHERE target_id=? AND timestamp_us>=? AND timestamp_us<?" + selection, (target_id, start_us, end_us)).fetchone()
        stats = dict(row)
        pair_filter = " AND speedtest_id IS NULL AND prev_speedtest_id IS NULL" if exclude_speedtests else ""
        pair = self.connection.execute("WITH ordered AS (SELECT p.status,p.rtt_ms,r.run_id,r.mono,r.speedtest_id,LAG(r.speedtest_id) OVER (ORDER BY r.rowid) AS prev_speedtest_id,LAG(p.status) OVER (ORDER BY r.rowid) AS prev_status,LAG(p.rtt_ms) OVER (ORDER BY r.rowid) AS prev_rtt,LAG(r.run_id) OVER (ORDER BY r.rowid) AS prev_run,LAG(r.mono) OVER (ORDER BY r.rowid) AS prev_mono FROM probe_samples p JOIN rounds r ON r.id=p.round_id WHERE p.target_id=? AND p.timestamp_us>=? AND p.timestamp_us<?) SELECT AVG(ABS(rtt_ms-prev_rtt)),COUNT(*) FROM ordered WHERE status='success' AND prev_status='success' AND run_id=prev_run AND mono-prev_mono>=0 AND mono-prev_mono<=?" + pair_filter, (target_id, start_us, end_us, interval * 1.75)).fetchone()
        stats.update(loss=stats["lost"] / stats["sent"] if stats["sent"] else None, availability=stats["received"] / stats["sent"] if stats["sent"] else None, jitter_ms=pair[0], jitter_pairs=pair[1])
        return stats

    def speedtests(self, start_us, end_us, limit=1000, after_rowid=0):
        if not 1 <= limit <= 1000:
            raise ValueError("Limite de página deve ser entre 1 e 1000")
        result = self.connection.execute("SELECT COUNT(*) AS samples,COALESCE(SUM(status='success'),0) AS successful,AVG(download_mbps) AS download_mean_mbps,MAX(download_mbps) AS download_max_mbps,AVG(upload_mbps) AS upload_mean_mbps,MAX(upload_mbps) AS upload_max_mbps,COALESCE(SUM(download_bytes+upload_bytes),0) AS transferred_bytes FROM speedtest_samples WHERE timestamp_us>=? AND timestamp_us<?", (start_us, end_us)).fetchone()
        items = []
        for row in self.connection.execute("SELECT rowid AS cursor,* FROM speedtest_samples WHERE timestamp_us>=? AND timestamp_us<? AND rowid>? ORDER BY rowid LIMIT ?", (start_us, end_us, after_rowid, limit)):
            item = dict(row)
            item["server"] = json.loads(item.pop("server_json"))
            for direction in ("download", "upload"):
                speed = item[direction + "_mbps"]
                item[direction + "_plan_percent"] = speed / item["plan_" + direction + "_mbps"] * 100 if speed is not None else None
            item["public_ip_matches_expected"] = item["public_ip"] == item["expected_public_ip"] if item["public_ip"] and item["expected_public_ip"] else None
            items.append(item)
        remaining = self.connection.execute("SELECT COUNT(*) FROM speedtest_samples WHERE timestamp_us>=? AND timestamp_us<? AND rowid>?", (start_us, end_us, after_rowid)).fetchone()[0]
        return dict(result, results=items, next_cursor=items[-1]["cursor"] if remaining > len(items) else None)

    def gaps(self, start_us, end_us, limit=1000, after_id=0):
        if not 1 <= limit <= 1000:
            raise ValueError("Limite de página deve ser entre 1 e 1000")
        return [dict(row) for row in self.connection.execute("SELECT * FROM gaps WHERE timestamp_us>=? AND timestamp_us<? AND id>? ORDER BY id LIMIT ?", (start_us, end_us, after_id, limit))]

    def incidents(self, start_us, end_us, limit=1000, after_id=0):
        if not 1 <= limit <= 1000:
            raise ValueError("Limite de página deve ser entre 1 e 1000")
        result = []
        for row in self.connection.execute("SELECT * FROM incidents WHERE started_us<? AND (ended_us IS NULL OR ended_us>?) AND id>? ORDER BY id LIMIT ?", (end_us, start_us, after_id, limit)):
            item = dict(row)
            clipped_start = max(start_us, item["started_us"])
            clipped_end = min(end_us, item["ended_us"] if item["ended_us"] is not None else end_us)
            observed = 0.0
            for segment in self.connection.execute("SELECT * FROM incident_segments WHERE incident_id=? AND ((end_us>? AND start_us<?) OR end_us<start_us)", (item["id"], start_us, end_us)):
                if segment["end_us"] <= segment["start_us"]:
                    continue
                overlap = max(0, min(end_us, segment["end_us"]) - max(start_us, segment["start_us"]))
                observed += segment["seconds"] * overlap / (segment["end_us"] - segment["start_us"])
            item.update(open=item["ended_us"] is None, clipped_start_us=clipped_start, clipped_end_us=clipped_end, civil_span_seconds=max(0, clipped_end - clipped_start) / 1_000_000, observed_seconds_in_window=observed)
            result.append(item)
        return result
