import tempfile
from pathlib import Path
import unittest
from bluesync.service import AudioError, SINK, Service, config, eligible


def sink(name, index, address=None, profile="a2dp-sink"):
    return {"name": name, "index": index, "properties": {} if address is None else {
        "device.api": "bluez5", "api.bluez5.address": address, "api.bluez5.profile": profile}}


class Audio:
    def __init__(self):
        self.sinks = [sink("speakers", 1)]
        self.modules = []
        self.default = "speakers"
        self.streams = [{"index": 10, "sink": 1, "properties": {}}]
        self.calls = []

    def info(self):
        return {"server_name": "PulseAudio (on PipeWire)", "default_sink_name": self.default}

    def listing(self, kind):
        return {"sinks": self.sinks, "modules": self.modules, "sink-inputs": self.streams}[kind].copy()

    def call(self, *args):
        self.calls.append(args)
        if args[0] == "set-default-sink":
            self.default = args[1]
        elif args[0] == "move-sink-input":
            stream = next(s for s in self.streams if s["index"] == args[1])
            stream["sink"] = next(s["index"] for s in self.sinks if s["name"] == args[2])
        elif args[0] == "load-module":
            self.modules.append({"index": 100, "name": args[1], "argument": " ".join(args[2:])})
            self.sinks.append(sink(SINK, 100))
            self.streams.append({"index": 101, "sink": 2, "owner_module": 100, "properties": {}})
            return "100"
        elif args[0] == "unload-module":
            self.modules = [m for m in self.modules if m["index"] != args[1]]
            self.sinks = [s for s in self.sinks if s["name"] != SINK]
            self.streams = [s for s in self.streams if s.get("owner_module") != args[1]]
        return ""


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.audio = Audio()
        self.settings = config(Path("/nonexistent/bluesync.toml"))
        self.service = Service(self.audio, self.settings)
        self.a = sink("bluez_output.A", 2, "AA:AA:AA:AA:AA:AA")
        self.b = sink("bluez_output.B", 3, "BB:BB:BB:BB:BB:BB")

    def test_full_connection_lifecycle_and_no_feedback(self):
        self.service.reconcile()
        self.assertEqual(self.audio.default, "speakers")
        self.audio.sinks.append(self.a)
        self.service.reconcile()
        self.assertEqual(self.audio.default, self.a["name"])
        self.audio.sinks.append(self.b)
        self.service.reconcile()
        self.assertEqual(self.audio.default, SINK)
        self.assertEqual(self.audio.streams[0]["sink"], 100)
        self.assertEqual(self.audio.streams[1]["sink"], 2)
        calls = len(self.audio.calls)
        self.service.reconcile()
        self.assertEqual(len(self.audio.calls), calls)
        self.audio.sinks.remove(self.a)
        self.service.reconcile()
        self.assertEqual(self.audio.default, self.b["name"])
        self.assertEqual(self.audio.streams[0]["sink"], 3)
        self.assertEqual(self.audio.modules, [])
        self.audio.sinks.remove(self.b)
        self.service.reconcile()
        self.assertEqual(self.audio.default, "speakers")

    def test_restart_adopts_existing_module(self):
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        replacement = Service(self.audio, self.settings)
        replacement.reconcile()
        self.assertEqual(len(self.audio.modules), 1)
        self.assertEqual(sum(c[0] == "load-module" for c in self.audio.calls), 1)
        replacement.cleanup()
        self.assertEqual(self.audio.modules, [])
        self.assertEqual(self.audio.default, "speakers")

    def test_server_restart_recreates_module(self):
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        self.audio.sinks = [sink("speakers", 1), self.a, self.b]
        self.audio.modules = []
        self.audio.streams = []
        self.service.reconcile()
        self.assertEqual(len(self.audio.modules), 1)
        self.assertEqual(self.audio.default, SINK)

    def test_membership_change_rebuilds(self):
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        self.audio.sinks.append(sink("bluez_output.C", 4, "CC:CC:CC:CC:CC:CC"))
        self.service.reconcile()
        self.assertIn("bluez_output.C", self.audio.modules[0]["argument"])
        self.assertEqual(len(self.audio.modules), 1)

    def test_new_application_is_routed(self):
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        self.audio.streams.append({"index": 11, "sink": 1, "properties": {}})
        self.service.reconcile()
        self.assertEqual(self.audio.streams[-1]["sink"], 100)

    def test_foreign_sink_collision_is_not_removed(self):
        self.audio.sinks += [self.a, self.b, sink(SINK, 90)]
        with self.assertRaises(AudioError):
            self.service.reconcile()
        self.assertEqual(self.audio.calls, [])

    def test_filter_allowlist_profile_and_duplicate_device(self):
        self.settings["devices"] = ["aa:aa:aa:aa:aa:aa"]
        self.assertEqual(eligible([self.a, self.b, self.a], self.settings), (self.a["name"],))
        self.a["properties"]["api.bluez5.profile"] = "headset-head-unit"
        self.assertEqual(eligible([self.a], self.settings), ())
        self.settings["a2dp_only"] = False
        self.assertEqual(eligible([self.a], self.settings), (self.a["name"],))

    def test_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ("poll_interval = nan", "devices = ['oops']", "move_streams = 'false'", "unknown = 1"):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    config(path)


if __name__ == "__main__":
    unittest.main()

class PactlCompatibilityTests(unittest.TestCase):
    def test_missing_json_module_index(self):
        from unittest.mock import patch
        from bluesync.service import Pactl
        import json
        audio = Pactl()
        args = f"sink_name={SINK} sinks=bluez_output.A,bluez_output.B"
        with patch.object(audio, "call", side_effect=[
            json.dumps([{"name": "module-combine-sink", "argument": args}]),
            f"536870916\tmodule-combine-sink\t{args}\t\n",
        ]):
            self.assertEqual(audio.listing("modules")[0]["index"], 536870916)

    def test_disappearing_module_is_retried(self):
        from unittest.mock import patch
        from bluesync.service import Pactl
        audio = Pactl()
        with patch.object(audio, "call", side_effect=[
            '[{"name":"module-combine-sink","argument":"sink_name=bluesync_combined"}]', "",
        ]):
            with self.assertRaises(AudioError):
                audio.listing("modules")

class ReconnectTests(ServiceTests):
    def test_fast_reconnect_rebuilds_then_delays_transport_restart(self):
        from unittest.mock import patch
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        self.b["index"] = 33
        with patch("bluesync.service.time.monotonic", return_value=10):
            self.service.reconcile()
        self.assertFalse(any(c[0] == "suspend-sink" for c in self.audio.calls))
        self.assertEqual(sum(c[0] == "load-module" for c in self.audio.calls), 2)
        with patch("bluesync.service.time.monotonic", return_value=14):
            self.service.reconcile()
        self.assertEqual(self.audio.calls[-1], ("suspend-sink", self.b["name"], "1"))
        with patch("bluesync.service.time.monotonic", return_value=16):
            self.service.reconcile()
        self.assertEqual(self.audio.calls[-1], ("suspend-sink", self.b["name"], "0"))
        calls = len(self.audio.calls)
        self.service.reconcile()
        self.assertEqual(len(self.audio.calls), calls)

    def test_disconnect_during_recovery_cancels_stale_instance(self):
        from unittest.mock import patch
        self.audio.sinks += [self.a, self.b]
        self.service.reconcile()
        self.audio.sinks.remove(self.b)
        self.service.reconcile()
        self.audio.sinks.append(self.b)
        with patch("bluesync.service.time.monotonic", return_value=10):
            self.service.reconcile()
        self.audio.sinks.remove(self.b)
        with patch("bluesync.service.time.monotonic", return_value=20):
            self.service.reconcile()
        self.assertFalse(any(c[0] == "suspend-sink" for c in self.audio.calls))
        self.assertFalse(self.service.recovery)
