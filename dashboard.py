import argparse
import json
import logging
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from collector import ConfigError, load_config
from database import History


ROOT = Path(__file__).resolve().parent
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 604800}
LOG = logging.getLogger("netmonitor.dashboard")


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def freshness(item, now_us, interval):
    if item is None:
        return None
    delta = (now_us - item["timestamp_us"]) / 1_000_000
    item.update(age_seconds=max(0, delta), clock_anomaly=delta < -1,
                stale=delta < -1 or delta > interval * 3)
    return item


def speedtest_item(row):
    if row is None:
        return None
    item = dict(row)
    item["server"] = json.loads(item.pop("server_json"))
    for direction in ("download", "upload"):
        value = item[direction + "_mbps"]
        plan = item["plan_" + direction + "_mbps"]
        item[direction + "_plan_percent"] = value / plan * 100 if value is not None and plan else None
    item["public_ip_matches_expected"] = (item["public_ip"] == item["expected_public_ip"]
                                           if item["public_ip"] and item["expected_public_ip"] else None)
    return item


class Dashboard:
    def __init__(self, settings, timezone="America/Sao_Paulo", clock=None):
        self.settings = settings
        self.timezone = timezone
        self.clock = clock or (lambda: time.time_ns() // 1000)
        self.cache = {}
        self.history_lock = threading.Lock()

    def open_history(self):
        path = Path(self.settings["database_path"])
        if not path.is_file():
            raise APIError(503, "database_missing", "O banco de monitoramento ainda não existe. Inicie o coletor com a configuração correspondente.")
        history = History(path)
        try:
            history.connection.execute("BEGIN")
            versions = [row[0] for row in history.connection.execute("SELECT version FROM schema_version")]
            if sorted(versions) != [1, 2, 3]:
                raise APIError(503, "schema_unsupported", "Versão do banco incompatível. A atualização do banco é responsabilidade do coletor.")
            return history
        except Exception:
            history.close()
            raise

    def status(self):
        now = self.clock()
        with self.open_history() as history:
            connection = history.connection
            targets = [dict(row) for row in connection.execute("SELECT * FROM targets")]
            row = connection.execute("SELECT r.*, c.interval_seconds FROM rounds r JOIN collector_runs c ON c.id=r.run_id ORDER BY r.rowid DESC LIMIT 1").fetchone()
            latest = freshness(dict(row) if row else None, now,
                               row["interval_seconds"] if row else self.settings["icmp_interval_seconds"])
            if latest:
                latest["link"] = json.loads(latest.pop("link_json"))
                latest["samples"] = [dict(sample) for sample in connection.execute("SELECT * FROM probe_samples WHERE round_id=?", (latest["id"],))]
                latest["current_diagnosis"] = "unknown" if latest["stale"] else latest["diagnosis"]
            resolvers = {}
            ids = {row[0] for row in connection.execute("SELECT DISTINCT resolver_id FROM dns_samples")}
            ids.update(resolver["id"] for resolver in self.settings["dns"]["resolvers"])
            for resolver_id in sorted(ids):
                row = connection.execute("SELECT * FROM dns_samples WHERE resolver_id=? ORDER BY rowid DESC LIMIT 1", (resolver_id,)).fetchone()
                item = freshness(dict(row) if row else None, now, self.settings["dns"]["interval_seconds"])
                if item:
                    item["answers"] = json.loads(item.pop("answers_json"))
                    item["current_status"] = "unknown" if item["stale"] else item["status"]
                resolvers[resolver_id] = item
            last = speedtest_item(connection.execute("SELECT * FROM speedtest_samples ORDER BY rowid DESC LIMIT 1").fetchone())
            success = speedtest_item(connection.execute("SELECT * FROM speedtest_samples WHERE status='success' ORDER BY rowid DESC LIMIT 1").fetchone())
            return {"generated_us": now, "timezone": self.timezone, "targets": targets,
                    "latest_icmp": latest, "latest_dns": resolvers,
                    "latest_speedtest": last, "latest_successful_speedtest": success,
                    "open_incidents": [dict(row) for row in connection.execute("SELECT * FROM incidents WHERE ended_us IS NULL ORDER BY id DESC")],
                    "interface": self.settings["interface"], "dns_interval_seconds": self.settings["dns"]["interval_seconds"],
                    "speedtest_enabled": self.settings["speedtest"]["enabled"]}

    def history(self, window):
        if window not in WINDOWS:
            raise APIError(400, "invalid_window", "Período deve ser 1h, 24h ou 7d.")
        with self.history_lock:
            cached = self.cache.get(window)
            if cached and time.monotonic() - cached[0] < 30:
                return cached[1]
            now = self.clock()
            with self.open_history() as history:
                result = history.window(window, now, self.settings["icmp_interval_seconds"],
                                        self.settings["dns"]["interval_seconds"], buckets=120)
                result.update(generated_us=now, timezone=self.timezone, window=window)
                result.pop("latest_icmp", None)
                result.pop("latest_dns", None)
                result["intervals"] = {"icmp_seconds": self.settings["icmp_interval_seconds"],
                                       "dns_seconds": self.settings["dns"]["interval_seconds"]}
            self.cache[window] = (time.monotonic(), result)
            return result

    def page(self, kind, params):
        allowed = {"start_us", "end_us", "cursor", "limit"}
        if set(params) - allowed or any(len(values) != 1 for values in params.values()):
            raise APIError(400, "invalid_parameters", "Parâmetros de paginação inválidos.")
        try:
            start = int(params["start_us"][0])
            end = int(params["end_us"][0])
            cursor = int(params.get("cursor", ["0"])[0])
            limit = int(params.get("limit", ["100"])[0])
            if not 0 <= start < end <= 2**63 - 1 or end - start > WINDOWS["7d"] * 1_000_000 or cursor < 0 or cursor > 2**63 - 1 or not 1 <= limit <= 1000:
                raise ValueError()
        except (KeyError, ValueError):
            raise APIError(400, "invalid_parameters", "Informe uma janela de até 7 dias, cursor não negativo e limite entre 1 e 1000.")
        with self.open_history() as history:
            if kind == "speedtests":
                return history.speedtests(start, end, limit, cursor)
            method = history.incidents if kind == "incidents" else history.gaps
            items = method(start, end, limit, cursor)
            more = method(start, end, 1, items[-1]["id"]) if items else []
            return {"items": items, "next_cursor": items[-1]["id"] if more else None}

    def api(self, path, params):
        try:
            if path == "/api/status" and not params:
                return self.status()
            if path == "/api/history":
                if set(params) - {"window"} or len(params.get("window", ["24h"])) != 1:
                    raise APIError(400, "invalid_parameters", "Parâmetros de histórico inválidos.")
                return self.history(params.get("window", ["24h"])[0])
            if path in ("/api/incidents", "/api/gaps", "/api/speedtests"):
                return self.page(path.rsplit("/", 1)[1], params)
            raise APIError(404, "not_found", "Consulta não encontrada.")
        except sqlite3.Error as exc:
            LOG.warning("Falha de leitura SQLite: %s", exc)
            raise APIError(503, "database_unavailable", "Não foi possível ler o banco. Verifique sua integridade, versão e permissões.") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("Dados de monitoramento indisponíveis: %s", exc)
            raise APIError(503, "data_unavailable", "Os dados de monitoramento não puderam ser interpretados.") from exc


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, dashboard):
        self.dashboard = dashboard
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "Netmonitor"

    def send_payload(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, result):
        self.send_payload(status, json.dumps(result, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        port = self.server.server_port
        hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin and origin != "http://" + host):
            self.send_json(403, {"error": {"code": "local_access_only", "message": "Acesso permitido apenas pelo endereço local do dashboard."}})
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path.startswith("/api/"):
                result = self.server.dashboard.api(parsed.path, parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20))
                self.send_json(200, result)
                return
            assets = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                      "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if parsed.path not in assets:
                raise APIError(404, "not_found", "Página não encontrada.")
            filename, content_type = assets[parsed.path]
            self.send_payload(200, (ROOT / "web" / filename).read_bytes(), content_type + "; charset=utf-8")
        except APIError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError:
            self.send_json(400, {"error": {"code": "invalid_parameters", "message": "Parâmetros inválidos."}})
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format, *args):
        LOG.debug(format, *args)


def load_dashboard_config(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"host", "port", "timezone", "collector_config"}:
        raise ValueError("Configuração do dashboard deve conter host, port, timezone e collector_config.")
    if config["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("Esta versão usa somente loopback (127.0.0.1 ou localhost).")
    if isinstance(config["port"], bool) or not isinstance(config["port"], int) or not 1024 <= config["port"] <= 65535:
        raise ValueError("Porta deve ser um inteiro entre 1024 e 65535.")
    if not isinstance(config["timezone"], str):
        raise ValueError("Fuso inválido.")
    ZoneInfo(config["timezone"])
    if not isinstance(config["collector_config"], str) or not config["collector_config"].strip():
        raise ValueError("Informe collector_config.")
    collector_path = Path(config["collector_config"]).expanduser()
    config["collector_config"] = str(collector_path if collector_path.is_absolute() else path.parent / collector_path)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dashboard local de leitura do Netmonitor")
    parser.add_argument("--config", default="dashboard.json", help="Configuração do dashboard")
    parser.add_argument("--collector-config", help="Configuração usada pelo coletor; substitui collector_config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_dashboard_config(args.config)
        settings = load_config(args.collector_config or config["collector_config"])
        server = DashboardServer((config["host"], config["port"]), Dashboard(settings, config["timezone"]))
    except (OSError, ValueError, ConfigError, ZoneInfoNotFoundError) as exc:
        LOG.error("Não foi possível iniciar o dashboard: %s", exc)
        return 2
    LOG.info("Dashboard em http://localhost:%s — encerre com Ctrl+C", server.server_port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
