import argparse
import asyncio
import contextlib
import fcntl
import ipaddress
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from database import Database, History
from speedtest import SpeedtestProbe, validate_speedtest


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("netmonitor")


class ConfigError(ValueError):
    pass


def number(value, name, minimum=0, maximum=86400, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= minimum or value > maximum or (integer and not isinstance(value, int)):
        raise ConfigError("Valor inválido para " + name)
    return value


def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ConfigError("Identificador inválido: " + name)
    return value


def load_config(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    try:
        with path.open(encoding="utf-8") as source:
            config = json.load(source)
    except (OSError, ValueError) as exc:
        raise ConfigError("Não foi possível ler JSON de configuração: " + str(exc)) from exc
    if not isinstance(config, dict):
        raise ConfigError("A configuração deve ser um objeto JSON")
    required = {"targets", "icmp_interval_seconds", "icmp_timeout_ms", "process_timeout_seconds", "fping_binary", "dns", "interface", "database_path", "retention_days", "incident_retention_days", "maintenance_interval_seconds", "retention_batch_size", "failure_threshold", "recovery_threshold", "flush_interval_seconds", "max_queue_size", "log_level"}
    fields = set(config) - {"speedtest"}
    if fields != required:
        raise ConfigError("Campos ausentes/desconhecidos: " + str(sorted(required.symmetric_difference(fields))))
    try:
        config["speedtest"] = validate_speedtest(config.get("speedtest", {}), path)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    targets = config["targets"]
    if not isinstance(targets, list) or not 3 <= len(targets) <= 32:
        raise ConfigError("Configure de 3 a 32 alvos, um gateway e pelo menos dois WAN")
    ids, addresses = set(), set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"id", "name", "address", "role"}:
            raise ConfigError("Estrutura de alvo inválida")
        identifier(target["id"], "target.id")
        if not isinstance(target["name"], str) or not target["name"].strip() or len(target["name"]) > 128:
            raise ConfigError("Nome de alvo inválido")
        if not isinstance(target["address"], str):
            raise ConfigError("Endereço do alvo deve ser texto")
        try:
            address = str(ipaddress.ip_address(target["address"]))
        except (ValueError, TypeError) as exc:
            raise ConfigError("Alvo deve usar IP numérico") from exc
        if target["role"] not in ("gateway", "wan") or target["id"] in ids or address in addresses:
            raise ConfigError("Papel inválido ou alvo duplicado")
        target["address"] = address
        ids.add(target["id"])
        addresses.add(address)
    if sum(t["role"] == "gateway" for t in targets) != 1 or sum(t["role"] == "wan" for t in targets) < 2:
        raise ConfigError("Exige um gateway e pelo menos dois WAN")
    for key in ("icmp_interval_seconds", "process_timeout_seconds", "flush_interval_seconds", "maintenance_interval_seconds"):
        number(config[key], key)
    for key, maximum in (("icmp_timeout_ms", 60000), ("retention_days", 3650), ("incident_retention_days", 36500), ("retention_batch_size", 10000), ("failure_threshold", 1000), ("recovery_threshold", 1000), ("max_queue_size", 4096)):
        number(config[key], key, maximum=maximum, integer=True)
    budget = config["icmp_timeout_ms"] / 1000 + len(targets) * 0.01 + 0.1
    if not budget < config["process_timeout_seconds"] <= config["icmp_interval_seconds"]:
        raise ConfigError("Timeout global deve caber no intervalo e exceder timeout por alvo + espaçamento de 10 ms/alvo + margem de 100 ms")
    if config["flush_interval_seconds"] > config["icmp_interval_seconds"]:
        raise ConfigError("Flush deve ocorrer pelo menos uma vez por intervalo ICMP")
    dns = config["dns"]
    if not isinstance(dns, dict) or set(dns) != {"interval_seconds", "timeout_seconds", "name", "record_type", "resolvers"}:
        raise ConfigError("Estrutura DNS inválida")
    number(dns["interval_seconds"], "dns.interval_seconds")
    number(dns["timeout_seconds"], "dns.timeout_seconds", maximum=60)
    if dns["timeout_seconds"] >= dns["interval_seconds"]:
        raise ConfigError("Timeout DNS deve ser menor que seu intervalo")
    name = dns["name"]
    if not isinstance(name, str) or len(name) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in name.rstrip(".").split(".")):
        raise ConfigError("Nome DNS inválido")
    if dns["record_type"] not in ("A", "AAAA", "MX", "TXT", "NS", "SOA", "CNAME", "PTR", "SRV"):
        raise ConfigError("Tipo de registro DNS não suportado")
    resolvers = dns["resolvers"]
    if not isinstance(resolvers, list) or not 2 <= len(resolvers) <= 8:
        raise ConfigError("Configure de 2 a 8 caminhos DNS")
    ids, addresses = set(), set()
    for resolver in resolvers:
        if not isinstance(resolver, dict) or set(resolver) != {"id", "address"}:
            raise ConfigError("Estrutura de resolver inválida")
        identifier(resolver["id"], "resolver.id")
        address = resolver["address"]
        if not isinstance(address, str):
            raise ConfigError("Endereço do resolver deve ser texto")
        if address != "system":
            try:
                address = str(ipaddress.ip_address(address))
            except (ValueError, TypeError) as exc:
                raise ConfigError("Resolver deve ser 'system' ou IP numérico") from exc
            resolver["address"] = address
        if resolver["id"] in ids or address in addresses:
            raise ConfigError("Resolver duplicado")
        ids.add(resolver["id"])
        addresses.add(address)
    if "system" not in addresses or len(addresses) < 2:
        raise ConfigError("DNS exige caminho system e resolver explícito")
    for key in ("fping_binary", "database_path", "interface"):
        if not isinstance(config[key], str) or not config[key] or "\x00" in config[key]:
            raise ConfigError("Texto inválido em " + key)
    identifier(config["interface"], "interface")
    if config["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("Nível de log inválido")
    for key in ("database_path", "fping_binary"):
        item = Path(config[key]).expanduser()
        if key == "database_path" or "/" in config[key]:
            config[key] = str((item if item.is_absolute() else path.parent / item).resolve())
    if config["retention_days"] < 7:
        LOG.warning("Retenção abaixo de 7 dias limita as consultas de histórico de 7d")
    return config


class ProcessLock:
    def __init__(self, database_path):
        self.path = Path(str(database_path) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Outra instância já usa este banco") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class ProcessRunner:
    def __init__(self, spawn=asyncio.create_subprocess_exec):
        self.spawn = spawn

    async def run(self, arguments, timeout):
        environment = dict(os.environ, LC_ALL="C", LANG="C")
        try:
            process = await self.spawn(*arguments, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True)
        except OSError as exc:
            return None, "", str(exc), "spawn_error: " + str(exc)
        async def read(stream):
            output = bytearray()
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return bytes(output)
                output.extend(chunk)
                if len(output) > 65536:
                    raise ValueError("Saída do subprocesso excedeu 64 KiB")
        async def communicate():
            readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
            try:
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
                return stdout, stderr
            finally:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
        task = asyncio.create_task(communicate())
        try:
            stdout, stderr = await asyncio.wait_for(task, timeout)
            return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), None
        except (asyncio.TimeoutError, ValueError) as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.stop(process)
            return None, "", "", "process_timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.stop(process)
            raise

    async def stop(self, process):
        async def drain(stream):
            while await stream.read(4096):
                pass
        drains = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
        try:
            await self._terminate(process)
        finally:
            await asyncio.gather(*drains, return_exceptions=True)

    async def _terminate(self, process):
        if process.returncode is None:
            self._signal(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except asyncio.TimeoutError:
                self._signal(process, signal.SIGKILL)
                await process.wait()
        else:
            await process.wait()

    def _signal(self, process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(sig)


def parse_fping(targets, returncode, stdout, stderr, error=None):
    operational = error
    if returncode not in (0, 1) and not operational:
        operational = "fping exit=" + str(returncode) + ": " + (stderr or stdout).strip()[:1000]
    found = {}
    addresses = {t["address"] for t in targets}
    unexpected = []
    if not operational:
        for line in (stdout + "\n" + stderr).splitlines():
            if not line.strip():
                continue
            address, separator, value = line.strip().rpartition(" : ")
            address, value = address.strip(), value.strip()
            if not separator or address not in addresses or address in found:
                unexpected.append(line[:200])
                continue
            if value == "-":
                found[address] = ("loss", None, None)
            else:
                try:
                    rtt = float(value)
                    if not re.fullmatch(r"\d+(?:\.\d+)?", value) or not math.isfinite(rtt) or rtt < 0:
                        raise ValueError("RTT inválido")
                    found[address] = ("success", rtt, None)
                except ValueError:
                    found[address] = ("operational_error", None, "RTT/saída inválida: " + value[:200])
        if unexpected:
            operational = "Saída inesperada: " + "; ".join(unexpected)[:1000]
    result = []
    for target in targets:
        status, rtt, target_error = ("operational_error", None, operational) if operational else found.get(target["address"], ("operational_error", None, "Resultado ausente"))
        result.append({"target_id": target["id"], "status": status, "rtt_ms": rtt, "error": target_error})
    return result


class ICMPProbe:
    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or ProcessRunner()
        self.capability_error = None

    async def prepare(self):
        code, stdout, stderr, error = await self.runner.run([self.config["fping_binary"], "--help"], self.config["process_timeout_seconds"])
        help_text = stdout + stderr
        missing = [option for option in ("-C", "-q", "-t", "-p", "-r") if option not in help_text]
        self.capability_error = error or ("fping não suporta opções necessárias: " + str(missing) if code != 0 or missing else None)
        return self.capability_error

    async def collect(self):
        if self.capability_error:
            return parse_fping(self.config["targets"], None, "", "", self.capability_error)
        arguments = [self.config["fping_binary"], "-C", "1", "-q", "-r", "0", "-t", str(self.config["icmp_timeout_ms"]), "-p", str(max(10, self.config["icmp_timeout_ms"]))]
        arguments.extend(t["address"] for t in self.config["targets"])
        result = await self.runner.run(arguments, self.config["process_timeout_seconds"])
        return parse_fping(self.config["targets"], *result)


def diagnose(targets, samples):
    outcomes = {sample["target_id"]: True if sample["status"] == "success" else False if sample["status"] == "loss" else None for sample in samples}
    gateway = outcomes[next(t["id"] for t in targets if t["role"] == "gateway")]
    wan = [outcomes[t["id"]] for t in targets if t["role"] == "wan"]
    external = True if True in wan else False if all(value is False for value in wan) and gateway is True else None
    local = True if gateway is True or True in wan else False if gateway is False and all(value is False for value in wan) else None
    scopes = {"target:" + key: value for key, value in outcomes.items()}
    scopes.update(wan=external, local=local)
    if gateway is None or None in wan:
        diagnosis = "unknown"
    elif gateway and all(wan):
        diagnosis = "icmp_normal"
    elif gateway and any(wan):
        diagnosis = "target_or_path_degraded"
    elif gateway:
        diagnosis = "wan_unavailable_or_icmp_filtered"
    elif any(wan):
        diagnosis = "gateway_no_icmp_external_reachable"
    else:
        diagnosis = "local_link_or_gateway_wan_unknown"
    return diagnosis, scopes


def read_link(interface, root=Path("/sys/class/net")):
    result = {"interface": interface, "operstate": None, "carrier": None}
    for key in ("operstate", "carrier"):
        try:
            value = (root / interface / key).read_text().strip()
            result[key] = value if key == "operstate" else value == "1" if value in ("0", "1") else None
        except OSError:
            pass
    return result


class DNSProbe:
    def __init__(self, config, monotonic=time.monotonic):
        try:
            import dns.asyncresolver
            import dns.exception
            import dns.resolver
            import dns.rcode
        except ImportError as exc:
            raise RuntimeError("Instale dnspython no ambiente virtual: python -m pip install -r requirements.txt") from exc
        self.dns = dns
        self.config = config["dns"]
        self.monotonic = monotonic

    async def collect(self, resolver_config):
        started = self.monotonic()
        path = resolver_config["address"]
        status, error, answers = "operational_error", None, []
        try:
            resolver = self.dns.asyncresolver.Resolver(configure=path == "system")
            if path != "system":
                resolver.nameservers = [path]
            path = ",".join(str(server) for server in resolver.nameservers)
            answer = await asyncio.wait_for(resolver.resolve(self.config["name"], self.config["record_type"], lifetime=self.config["timeout_seconds"], search=False, raise_on_no_answer=False), self.config["timeout_seconds"])
            answers = [str(record) for record in answer]
            status = "success" if answers else "nodata"
            path = str(answer.nameserver) if answer.nameserver else path
        except self.dns.resolver.NXDOMAIN as exc:
            status, error = "nxdomain", str(exc)
        except (self.dns.exception.Timeout, asyncio.TimeoutError) as exc:
            status, error = "timeout", str(exc) or "Timeout DNS"
        except self.dns.resolver.NoNameservers as exc:
            errors = exc.kwargs.get("errors", [])
            rcodes = [entry[4].rcode() for entry in errors if len(entry) > 4 and hasattr(entry[4], "rcode")]
            status = "servfail" if self.dns.rcode.SERVFAIL in rcodes else "refused" if self.dns.rcode.REFUSED in rcodes else "dns_error"
            error = str(exc)
        except (self.dns.exception.DNSException, OSError, ValueError) as exc:
            error = str(exc)
        return {"resolver_id": resolver_config["id"], "path": path, "name": self.config["name"], "record_type": self.config["record_type"], "duration_ms": max(0, self.monotonic() - started) * 1000, "status": status, "error": error, "answers": answers}


class Clock:
    def monotonic(self):
        return time.monotonic()

    def utc_us(self):
        return time.time_ns() // 1000

    async def wait(self, stop, delay):
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass


def advance_deadline(deadline, now, interval):
    next_deadline = deadline + interval
    skipped = 0
    if next_deadline < now:
        skipped = math.floor((now - next_deadline) / interval) + 1
        next_deadline += skipped * interval
    return next_deadline, skipped


class Collector:
    def __init__(self, config, database, run_id, icmp=None, dns=None, clock=None, link_reader=read_link, speedtest=None):
        self.config = config
        self.database = database
        self.run_id = run_id
        self.clock = clock or Clock()
        self.icmp = icmp or ICMPProbe(config)
        self.dns = dns or DNSProbe(config, self.clock.monotonic)
        self.link_reader = link_reader
        self.stop = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=config["max_queue_size"])
        self.writer_stop = asyncio.Event()
        self.operational_error = False
        self.last_diagnosis = None
        self.last_dns = {}
        self.speedtest = speedtest or SpeedtestProbe(config, ProcessRunner())
        self.active_speedtest_id = None
        self.speedtest_sequence = 0
        self.last_speedtest_id = None

    async def emit(self, event):
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            self.operational_error = True
            LOG.critical("Fila cheia: coleta interrompida; evento não persistido: %s", event["kind"])
            raise RuntimeError("Fila de persistência cheia") from exc

    async def icmp_round(self):
        mono, timestamp = self.clock.monotonic(), self.clock.utc_us()
        speedtest_id = self.active_speedtest_id
        speedtest_sequence = self.speedtest_sequence
        samples = await self.icmp.collect()
        speedtest_id = speedtest_id or self.active_speedtest_id or (self.last_speedtest_id if speedtest_sequence != self.speedtest_sequence else None)
        diagnosis, scopes = diagnose(self.config["targets"], samples)
        if diagnosis != self.last_diagnosis:
            LOG.info("Diagnóstico ICMP: %s", diagnosis)
            self.last_diagnosis = diagnosis
        errors = [sample for sample in samples if sample["status"] == "operational_error"]
        if errors:
            self.operational_error = True
            LOG.error("Erro operacional ICMP: %s", errors)
        await self.emit({"kind": "icmp", "id": uuid.uuid4().hex, "timestamp_us": timestamp, "mono": mono, "samples": samples, "diagnosis": diagnosis, "scopes": scopes, "link": self.link_reader(self.config["interface"]), "speedtest_id": speedtest_id})

    async def dns_round(self):
        async def query(resolver):
            mono, timestamp = self.clock.monotonic(), self.clock.utc_us()
            speedtest_id = self.active_speedtest_id
            speedtest_sequence = self.speedtest_sequence
            sample = await self.dns.collect(resolver)
            sample["speedtest_id"] = speedtest_id or self.active_speedtest_id or (self.last_speedtest_id if speedtest_sequence != self.speedtest_sequence else None)
            if sample["status"] != self.last_dns.get(resolver["id"]):
                LOG.info("DNS %s via %s: %s (%s)", resolver["id"], sample["path"], sample["status"], sample.get("error"))
                self.last_dns[resolver["id"]] = sample["status"]
            if sample["status"] == "operational_error":
                self.operational_error = True
            sample.update(kind="dns", id=uuid.uuid4().hex, timestamp_us=timestamp, mono=mono)
            await self.emit(sample)
        tasks = [asyncio.create_task(query(resolver)) for resolver in self.config["dns"]["resolvers"]]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def speedtest_round(self):
        if self.active_speedtest_id is not None:
            raise RuntimeError("Speedtest sobreposto")
        test_id = uuid.uuid4().hex
        started_mono = self.clock.monotonic()
        self.active_speedtest_id = test_id
        self.last_speedtest_id = test_id
        self.speedtest_sequence += 1
        settings = self.config["speedtest"]
        await self.emit({"kind": "speedtest_start", "id": test_id, "timestamp_us": self.clock.utc_us(), "mono": started_mono, "interface": self.config["interface"], "plan_download_mbps": settings["plan_download_mbps"], "plan_upload_mbps": settings["plan_upload_mbps"], "ip_mode": settings["ip_mode"], "expected_public_ip": settings["expected_public_ip"]})
        LOG.info("Speedtest iniciado: %s", test_id)
        try:
            try:
                result = await self.speedtest.collect()
            except asyncio.CancelledError:
                await self.emit({"kind": "speedtest_end", "id": test_id, "timestamp_us": self.clock.utc_us(), "duration_seconds": max(0, self.clock.monotonic() - started_mono), "result": {"status": "cancelled", "error": "Coleta interrompida"}})
                LOG.info("Speedtest cancelado: %s", test_id)
                raise
            if result["status"] == "operational_error":
                self.operational_error = True
            await self.emit({"kind": "speedtest_end", "id": test_id, "timestamp_us": self.clock.utc_us(), "duration_seconds": max(0, self.clock.monotonic() - started_mono), "result": result})
            LOG.info("Speedtest concluído: %s down=%s Mb/s up=%s Mb/s erro=%s", result["status"], result.get("download_mbps"), result.get("upload_mbps"), result.get("error"))
            await self.clock.wait(self.stop, settings["cooldown_seconds"])
        finally:
            self.active_speedtest_id = None

    async def schedule(self, name, interval, operation, initial_delay=0):
        deadline = self.clock.monotonic() + initial_delay
        while not self.stop.is_set():
            await self.clock.wait(self.stop, max(0, deadline - self.clock.monotonic()))
            if self.stop.is_set():
                break
            lateness = max(0, self.clock.monotonic() - deadline)
            if lateness >= interval:
                deadline, skipped = advance_deadline(deadline - interval, self.clock.monotonic(), interval)
                await self.emit({"kind": "gap", "timestamp_us": self.clock.utc_us(), "reason": name + "_delay", "duration_seconds": lateness, "details": str(skipped) + " prazos perdidos"})
                LOG.warning("Atraso %s: %.3fs; %d prazos perdidos", name, lateness, skipped)
                continue
            if lateness > 0.05:
                await self.emit({"kind": "gap", "timestamp_us": self.clock.utc_us(), "reason": name + "_late", "duration_seconds": lateness})
            await operation()
            deadline, skipped = advance_deadline(deadline, self.clock.monotonic(), interval)
            if skipped:
                await self.emit({"kind": "gap", "timestamp_us": self.clock.utc_us(), "reason": name + "_skipped", "duration_seconds": skipped * interval, "details": str(skipped) + " rodadas não executadas"})
                LOG.warning("Coleta %s perdeu %d prazos", name, skipped)

    async def writer(self):
        maintenance = self.clock.monotonic()
        while not self.writer_stop.is_set() or not self.queue.empty():
            try:
                event = await asyncio.wait_for(self.queue.get(), self.config["flush_interval_seconds"])
            except asyncio.TimeoutError:
                continue
            events = [event]
            while len(events) < self.config["max_queue_size"] and not self.queue.empty():
                events.append(self.queue.get_nowait())
            try:
                self.database.write_events(events, self.run_id, self.config)
                if self.clock.monotonic() >= maintenance:
                    deleted = self.database.retain_batch(self.clock.utc_us(), self.config)
                    maintenance = self.clock.monotonic() + (1 if deleted >= self.config["retention_batch_size"] else self.config["maintenance_interval_seconds"])
                    if deleted:
                        LOG.info("Retenção removeu %d registros/lotes", deleted)
            except (sqlite3.Error, OSError) as exc:
                self.operational_error = True
                LOG.critical("Falha de persistência; saída controlada, lote de %d eventos pode não ter sido salvo: %s", len(events), exc)
                self.stop.set()
                raise
            finally:
                for _ in events:
                    self.queue.task_done()

    async def run(self, once=False, include_speedtest=False):
        writer = asyncio.create_task(self.writer())
        producers = []
        stopper = None
        try:
            error = await self.icmp.prepare()
            if error:
                LOG.error("Verificação de fping falhou: %s", error)
                await self.icmp_round()
                self.operational_error = True
            elif self.stop.is_set():
                return 0
            elif once:
                producers = [asyncio.create_task(self.icmp_round()), asyncio.create_task(self.dns_round())]
            else:
                producers = [asyncio.create_task(self.schedule("icmp", self.config["icmp_interval_seconds"], self.icmp_round)), asyncio.create_task(self.schedule("dns", self.config["dns"]["interval_seconds"], self.dns_round))]
            if producers and (include_speedtest or (not once and self.config["speedtest"]["enabled"])):
                speedtest_error = await self.speedtest.prepare()
                if speedtest_error:
                    LOG.error("Verificação de speedtest falhou: %s", speedtest_error)
                if once:
                    producers.append(asyncio.create_task(self.speedtest_round()))
                else:
                    settings = self.config["speedtest"]
                    producers.append(asyncio.create_task(self.schedule("speedtest", settings["interval_seconds"], self.speedtest_round, settings["initial_delay_seconds"])))
            if producers:
                stopper = asyncio.create_task(self.stop.wait())
                pending_producers = set(producers)
                while pending_producers:
                    done, _ = await asyncio.wait(pending_producers | {writer, stopper}, return_when=asyncio.FIRST_COMPLETED)
                    if writer in done:
                        writer.result()
                        raise RuntimeError("Escritor encerrou antes da coleta")
                    if stopper in done:
                        break
                    for task in done:
                        task.result()
                        pending_producers.discard(task)
        finally:
            self.stop.set()
            for task in producers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            if stopper:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
            self.writer_stop.set()
            await writer
        return 2 if self.operational_error else 0


async def execute(config, once, include_speedtest=False):
    with ProcessLock(config["database_path"]):
        database = Database(config["database_path"])
        run_id = None
        reason = "failure"
        try:
            collector = Collector(config, database, None)
            run_id = database.start_run(config, collector.clock.utc_us())
            collector.run_id = run_id
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, collector.stop.set)
            LOG.info("Coletor iniciado: run=%s banco=%s", run_id, config["database_path"])
            try:
                result = await collector.run(once, include_speedtest)
                reason = "operational_error" if result else "clean_stop"
                return result
            finally:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
        finally:
            try:
                if run_id:
                    database.end_run(run_id, time.time_ns() // 1000, reason)
                    LOG.info("Coletor encerrado: %s", reason)
            finally:
                database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor ICMP/DNS independente de interface gráfica")
    parser.add_argument("--config", default="config.json", help="Configuração; caminho relativo à raiz do projeto")
    parser.add_argument("--once", action="store_true", help="Uma rodada ICMP e DNS imediata, com prazo limitado; perdas não são erro de execução")
    parser.add_argument("--history", choices=("1h", "24h", "7d"), help="Consulta somente leitura em JSON")
    parser.add_argument("--speedtest", action="store_true", help="Inclui teste de banda explícito em --once; requer CLI oficial da Ookla")
    args = parser.parse_args(argv)
    if args.speedtest and (not args.once or args.history):
        parser.error("--speedtest exige --once e não pode ser usado com --history")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        logging.getLogger().setLevel(config["log_level"])
        if args.history:
            with History(config["database_path"]) as history:
                print(json.dumps(history.window(args.history, time.time_ns() // 1000, config["icmp_interval_seconds"], config["dns"]["interval_seconds"]), ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(execute(config, args.once, args.speedtest))
    except (ConfigError, RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        LOG.error("Não foi possível executar: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
