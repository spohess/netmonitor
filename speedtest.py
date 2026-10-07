import ipaddress
import json
import math
from pathlib import Path


DEFAULT_SPEEDTEST = {
    "enabled": False,
    "binary": "speedtest",
    "interval_seconds": 21600,
    "initial_delay_seconds": 300,
    "timeout_seconds": 180,
    "cooldown_seconds": 10,
    "server_id": None,
    "plan_download_mbps": 1000,
    "plan_upload_mbps": 1000,
    "ip_mode": "static",
    "expected_public_ip": None,
}


def validate_speedtest(value, config_path):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SPEEDTEST):
        raise ValueError("Campos de speedtest inválidos")
    settings = dict(DEFAULT_SPEEDTEST, **value)
    if not isinstance(settings["enabled"], bool):
        raise ValueError("speedtest.enabled deve ser booleano")
    limits = {
        "interval_seconds": (900, 604800),
        "initial_delay_seconds": (0, 604800),
        "timeout_seconds": (1, 600),
        "cooldown_seconds": (0, 300),
        "plan_download_mbps": (1, 100000),
        "plan_upload_mbps": (1, 100000),
    }
    for key, (minimum, maximum) in limits.items():
        number = settings[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not minimum <= number <= maximum or not math.isfinite(number):
            raise ValueError("Valor inválido para speedtest." + key)
    if settings["timeout_seconds"] + settings["cooldown_seconds"] >= settings["interval_seconds"]:
        raise ValueError("Timeout + cooldown do speedtest devem caber no intervalo")
    server_id = settings["server_id"]
    if server_id is not None and (isinstance(server_id, bool) or not isinstance(server_id, int) or server_id <= 0):
        raise ValueError("speedtest.server_id deve ser inteiro positivo ou nulo")
    binary = settings["binary"]
    if not isinstance(binary, str) or not binary or "\x00" in binary:
        raise ValueError("speedtest.binary inválido")
    if "/" in binary:
        path = Path(binary).expanduser()
        settings["binary"] = str((path if path.is_absolute() else config_path.parent / path).resolve())
    if settings["ip_mode"] not in ("static", "dynamic"):
        raise ValueError("speedtest.ip_mode deve ser static ou dynamic")
    expected = settings["expected_public_ip"]
    if expected is not None:
        if not isinstance(expected, str):
            raise ValueError("speedtest.expected_public_ip deve ser IP numérico ou nulo")
        settings["expected_public_ip"] = str(ipaddress.ip_address(expected))
    return settings


def measured_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Métrica inválida: " + name)
    return value


def parse_speedtest(returncode, stdout, stderr, error=None):
    empty = {"download_mbps": None, "upload_mbps": None, "download_bytes": None, "upload_bytes": None, "latency_ms": None, "public_ip": None, "server": {}}
    if error:
        return dict(empty, status="operational_error", error=error)
    if returncode != 0:
        return dict(empty, status="failed", error=("speedtest exit=" + str(returncode) + ": " + (stderr or stdout).strip())[:1000])
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict) or result.get("type") != "result":
            raise ValueError("JSON não é resultado do CLI oficial da Ookla")
        metrics = {}
        for direction in ("download", "upload"):
            bandwidth = measured_number(result[direction]["bandwidth"], direction + ".bandwidth")
            transferred = measured_number(result[direction]["bytes"], direction + ".bytes")
            if not isinstance(transferred, int) or transferred > 9223372036854775807:
                raise ValueError("Contagem de bytes deve ser inteira de 64 bits")
            metrics[direction + "_mbps"] = measured_number(bandwidth * 8 / 1_000_000, direction + "_mbps")
            metrics[direction + "_bytes"] = transferred
        latency = measured_number(result["ping"]["latency"], "ping.latency")
        public_ip = result.get("interface", {}).get("externalIp")
        if public_ip is not None:
            if not isinstance(public_ip, str):
                raise ValueError("IP externo inválido")
            public_ip = str(ipaddress.ip_address(public_ip))
        server = result["server"]
        if not isinstance(server, dict) or not isinstance(server.get("id"), int) or isinstance(server["id"], bool):
            raise ValueError("Servidor de teste inválido")
        return dict(metrics, status="success", latency_ms=latency, public_ip=public_ip, server=server, error=None)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return dict(empty, status="operational_error", error="Resultado speedtest inválido: " + str(exc))


class SpeedtestProbe:
    def __init__(self, config, runner):
        self.config = config["speedtest"]
        self.interface = config["interface"]
        self.runner = runner
        self.capability_error = None

    async def prepare(self):
        code, stdout, stderr, error = await self.runner.run([self.config["binary"], "--help"], 5)
        output = stdout + stderr
        supported = "ookla" in output.lower() and all(option in output for option in ("--format", "--interface", "--progress", "--server-id"))
        self.capability_error = error or ("Instale o CLI oficial Speedtest by Ookla; binário incompatível ou opções ausentes" if code != 0 or not supported else None)
        return self.capability_error

    async def collect(self):
        if self.capability_error:
            return parse_speedtest(None, "", "", self.capability_error)
        arguments = [self.config["binary"], "--format=json", "--progress=no", "--interface=" + self.interface]
        if self.config["server_id"] is not None:
            arguments.append("--server-id=" + str(self.config["server_id"]))
        return parse_speedtest(*(await self.runner.run(arguments, self.config["timeout_seconds"])))
