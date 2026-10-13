"""Reconcile ready Bluetooth sinks with a PipeWire combined output."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading
import time
import tomllib

LOG = logging.getLogger("bluesync")
SINK = "bluesync_combined"


class AudioError(RuntimeError):
    pass


class Pactl:
    def call(self, *args):
        try:
            result = subprocess.run(
                ["pactl", *map(str, args)], capture_output=True, text=True,
                timeout=8, env={**os.environ, "LC_ALL": "C.UTF-8"}, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AudioError(str(exc)) from exc
        if result.returncode:
            raise AudioError(result.stderr.strip())
        return result.stdout.strip()

    def listing(self, kind):
        items = json.loads(self.call("--format=json", "list", kind))
        if kind == "modules" and any("index" not in item for item in items):
            # Some pactl versions omit module indices from JSON. Match the
            # text listing by both name and arguments, never by list position.
            indices = {}
            for line in self.call("list", "short", "modules").splitlines():
                fields = line.split("\t")
                if len(fields) >= 3 and fields[0].isdigit():
                    indices[(fields[1], fields[2])] = int(fields[0])
            for item in items:
                if "index" not in item:
                    key = (item["name"], item.get("argument", ""))
                    if key in indices:
                        item["index"] = indices[key]
                    elif item["name"] == "module-combine-sink":
                        raise AudioError("Module changed during discovery; retrying")
        return items

    def info(self):
        return json.loads(self.call("--format=json", "info"))


def config(path):
    result = {"poll_interval": 2.0, "devices": [], "a2dp_only": True,
              "latency_compensate": True, "move_streams": True}
    if path.exists():
        with path.open("rb") as source:
            supplied = tomllib.load(source)
        if supplied.keys() - result.keys():
            raise ValueError("Unknown configuration keys: " + str(supplied.keys() - result.keys()))
        result.update(supplied)
    interval = result["poll_interval"]
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0.5:
        raise ValueError("poll_interval must be a finite number >= 0.5")
    for key in ("a2dp_only", "latency_compensate", "move_streams"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if not isinstance(result["devices"], list) or any(
        not isinstance(item, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", item)
        for item in result["devices"]
    ):
        raise ValueError("devices must be an array of Bluetooth MAC addresses")
    return result


def eligible(sinks, settings):
    selected = {}
    allowed = {address.upper() for address in settings["devices"]}
    for sink in sorted(sinks, key=lambda item: item["name"]):
        props = sink.get("properties", {})
        if props.get("device.api") != "bluez5" and props.get("device.bus") != "bluetooth":
            continue
        address = props.get("api.bluez5.address", props.get("device.string", "")).upper()
        if allowed and address not in allowed:
            continue
        if settings["a2dp_only"] and not props.get("api.bluez5.profile", "").startswith("a2dp"):
            continue
        name = sink["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            continue
        # At most one sink per physical device, including during profile changes.
        selected.setdefault(address or props.get("device.name", name), name)
    return tuple(sorted(selected.values()))


class Service:
    def __init__(self, audio, settings):
        self.audio = audio
        self.settings = settings
        self.previous = None
        self.target = None
        self.seen_devices = set()
        self.device_instances = {}
        self.combined_instances = None
        self.recovery = {}

    def recover(self, instances):
        now = time.monotonic()
        for name, (index, deadline, suspended) in list(self.recovery.items()):
            if instances.get(name) != index:
                del self.recovery[name]
                continue
            if now < deadline:
                continue
            if not suspended:
                self.audio.call("suspend-sink", name, "1")
                self.recovery[name] = (index, now + 1.0, True)
                LOG.info("Suspended reconnected output for transport recovery: %s", name)
            else:
                self.audio.call("suspend-sink", name, "0")
                del self.recovery[name]
                LOG.info("Resumed reconnected Bluetooth output: %s", name)

    def owned(self):
        return [module for module in self.audio.listing("modules")
                if module["name"] == "module-combine-sink"
                and f"sink_name={SINK}" in shlex.split(module.get("argument", ""))]

    def route(self, target, modules):
        if self.audio.info().get("default_sink_name") != target:
            self.audio.call("set-default-sink", target)
        if not self.settings["move_streams"]:
            return
        sinks = {item["name"]: item["index"] for item in self.audio.listing("sinks")}
        owned_ids = {str(module["index"]) for module in modules}
        for stream in self.audio.listing("sink-inputs"):
            props = stream.get("properties", {})
            # Never move combine/loopback/filter internals: that could create feedback.
            if (str(stream.get("owner_module")) in owned_ids
                    or str(props.get("node.virtual", "")).lower() == "true"
                    or str(props.get("stream.dont-remix", "")).lower() == "true"
                    or props.get("media.role") == "filter"):
                continue
            if stream.get("sink") != sinks.get(target):
                try:
                    self.audio.call("move-sink-input", stream["index"], target)
                except AudioError as exc:
                    LOG.warning("Cannot move stream %s: %s", stream["index"], exc)

    def remove(self, modules):
        for module in modules:
            self.audio.call("unload-module", module["index"])

    def reconcile(self):
        info = self.audio.info()
        if "PipeWire" not in info.get("server_name", ""):
            raise AudioError("BlueSync requires pipewire-pulse")
        sinks = self.audio.listing("sinks")
        names = {item["name"] for item in sinks}
        desired = eligible(sinks, self.settings)
        instances = {item["name"]: item["index"] for item in sinks if item["name"] in desired}
        for name, index in instances.items():
            if name in self.seen_devices and self.device_instances.get(name) != index:
                self.recovery[name] = (index, float("inf"), False)
        self.seen_devices.update(instances)
        self.device_instances = instances
        modules = self.owned()
        if not desired:
            if self.target or modules:
                fallback = self.previous if self.previous in names else next(
                    (item["name"] for item in sinks if item["name"] != SINK), None)
                if fallback:
                    self.route(fallback, modules)
                self.remove(modules)
                LOG.info("Bluetooth outputs disconnected; restored %s", fallback)
            self.target = None
            self.previous = None
            self.combined_instances = None
            return
        if self.previous is None:
            current = info.get("default_sink_name")
            self.previous = current if current != SINK else ""
        target = desired[0]
        if len(desired) >= 2:
            expected = "sinks=" + ",".join(desired)
            matching = len(modules) == 1 and expected in shlex.split(modules[0].get("argument", ""))
            changed = self.combined_instances is not None and self.combined_instances != instances
            if not matching or SINK not in names or changed:
                if SINK in names and not modules:
                    raise AudioError(f"Sink name {SINK} already in use by another owner")
                # Keep playback on a surviving device while rebuilding the graph.
                self.route(desired[0], modules)
                self.remove(modules)
                self.audio.call(
                    "load-module", "module-combine-sink", f"sink_name={SINK}", expected,
                    "sink_properties=device.description=BlueSync",
                    "channels=2", "channel_map=front-left,front-right",
                    "latency_compensate=" + str(self.settings["latency_compensate"]).lower(),
                )
                modules = self.owned()
            self.combined_instances = instances.copy()
            target = SINK
        self.route(target, modules)
        if len(desired) == 1:
            self.remove(modules)
            self.combined_instances = None
        if target != self.target:
            LOG.info("Audio output: %s (%s)", target, ", ".join(desired))
        self.target = target
        # Start the settling period only after routing has succeeded.
        for name, (index, deadline, suspended) in list(self.recovery.items()):
            if deadline == float("inf"):
                self.recovery[name] = (index, time.monotonic() + 3.0, suspended)
        self.recover(instances)

    def cleanup(self):
        for name, (_, _, suspended) in list(self.recovery.items()):
            if suspended:
                try:
                    self.audio.call("suspend-sink", name, "0")
                except AudioError:
                    LOG.warning("Could not resume disappearing output %s", name)
        modules = self.owned()
        if not self.target and not modules:
            return
        sinks = self.audio.listing("sinks")
        names = {item["name"] for item in sinks}
        fallback = self.previous if self.previous in names else next(
            (item["name"] for item in sinks if item["name"] != SINK), None)
        if fallback:
            self.route(fallback, modules)
        self.remove(modules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bluesync/config.toml"
    parser.add_argument("--config", type=Path, default=default)
    parser.add_argument("--status", action="store_true", help="Read-only audio and eligible device status")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = config(args.config)
        audio = Pactl()
        if args.status:
            print(json.dumps({"server": audio.info(), "eligible_sinks": eligible(audio.listing("sinks"), settings)}, indent=2))
            return
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise ValueError("XDG_RUNTIME_DIR is required; run in a user session")
        with (Path(runtime) / "bluesync.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("BlueSync is already running") from exc
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            service = Service(audio, settings)
            try:
                while not stop.is_set():
                    try:
                        service.reconcile()
                    except (AudioError, ValueError) as exc:
                        LOG.warning("Will retry: %s", exc)
                    stop.wait(settings["poll_interval"])
            finally:
                try:
                    service.cleanup()
                except (AudioError, ValueError) as exc:
                    LOG.warning("Cleanup unavailable: %s", exc)
    except (AudioError, ValueError, OSError) as exc:
        parser.exit(1, f"BlueSync: {exc}\n")


if __name__ == "__main__":
    main()
