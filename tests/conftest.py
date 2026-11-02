"""Stands in for the `orca` module and for Filament DB.

The stand-ins hold the rules that real testing found, because a stand-in that accepts anything would
pass a plugin that OrcaSlicer rejects. FakeBundles fails a reload the way the host does, a selected
preset's name finds the edited copy, and FakeFilamentDb follows the routes in Filament DB's source.
"""
import enum
import importlib
import json
import re
import sys
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUNDLE_ID = "filament-db"
PLAIN_ID = "a" * 24
CALIBRATED_ID = "b" * 24
FILAMENT_KEYS = [
    "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "close_fan_the_first_x_layers", "cool_plate_temp", "cool_plate_temp_initial_layer", "default_filament_colour",
    "enable_pressure_advance", "eng_plate_temp", "eng_plate_temp_initial_layer", "fan_cooling_layer_time",
    "fan_max_speed", "fan_min_speed", "filament_cost", "filament_density", "filament_deretraction_speed",
    "filament_diameter", "filament_end_gcode", "filament_flow_ratio", "filament_max_volumetric_speed",
    "filament_notes", "filament_ramming_parameters", "filament_retraction_length", "filament_retraction_speed",
    "filament_settings_id", "filament_shrink", "filament_shrinkage_compensation_z", "filament_type",
    "filament_vendor", "filament_z_hop", "hot_plate_temp", "hot_plate_temp_initial_layer", "inherits",
    "nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_high",
    "nozzle_temperature_range_low", "pressure_advance", "slow_down_layer_time", "slow_down_min_speed",
    "supertack_plate_temp", "supertack_plate_temp_initial_layer", "textured_cool_plate_temp",
    "textured_cool_plate_temp_initial_layer", "textured_plate_temp", "textured_plate_temp_initial_layer",
]
# Known to the config definitions, but not filament settings, so a preset holding one is invalid.
INVALID_IN_A_PRESET = {"filament_colour", "temperature"}
NULLABLE_KEYS = {"filament_retraction_length", "filament_retraction_speed", "filament_deretraction_speed", "filament_z_hop"}


class PluginResult(enum.Enum):
    Success = 0
    Skipped = 1


class ExecutionResult:
    def __init__(self, status, message):
        self.status = status
        self.message = message

    @staticmethod
    def success(message=""):
        return ExecutionResult(PluginResult.Success, message)

    @staticmethod
    def skipped(message=""):
        return ExecutionResult(PluginResult.Skipped, message)


class NotificationLevel(enum.Enum):
    RegularNotificationLevel = 0
    WarningNotificationLevel = 1


def serialized(value):
    """Format a preset file value the way Preset.config_value() returns it."""
    item = value[0]
    return f'"{item}"' if " " in item or "\n" in item else item


class FakePreset:
    def __init__(self, name, file="", values=None, is_dirty=False):
        self.name = name
        self.file = str(file)
        self.is_dirty = is_dirty
        self.values = dict.fromkeys(FILAMENT_KEYS, "0")
        self.values.update(values or {})

    def config_keys(self):
        return list(self.values)

    def config_value(self, key):
        return self.values.get(key)

    def label(self):
        return ("* " if self.is_dirty else "") + self.name.rsplit("/", 1)[-1]


class FakeFilaments:
    def __init__(self):
        self.presets = {}
        self.selected = FakePreset("Generic PLA @System")
        self.edited = self.selected

    def add(self, preset):
        self.presets[preset.name] = preset

    def select(self, name, edits=None):
        self.selected = self.presets[name]
        self.edited = FakePreset(name, self.selected.file, {**self.selected.values, **(edits or {})}, bool(edits))

    def edited_preset(self):
        return self.edited

    def selected_preset(self):
        return self.selected

    def find_preset(self, name):
        # The host's lookup hands back the edited copy for the selected preset.
        return self.edited if name == self.selected.name else self.presets.get(name)


class FakeBundles:
    """OrcaSlicer's local bundles, for the active user's folder only."""

    def __init__(self, data_dir, filaments, active_user):
        self.data_dir = data_dir
        self.filaments = filaments
        self.reloads = 0
        self.refuse = False
        self.activate(active_user)

    def activate(self, user):
        self.local_dir = self.data_dir / "user" / user / "_local"

    def list_ids(self):
        return [path.parent.name for path in self.local_dir.glob("*/bundle_metadata.json")]

    def reload_local(self, bundle_id):
        self.reloads += 1
        if self.refuse:
            raise RuntimeError("Local bundle contains an invalid preset")
        prefix = f"_local/{bundle_id}/"
        loaded = {}
        for path in (self.local_dir / bundle_id / "filament").glob("*.json"):
            profile = json.loads(path.read_text(encoding="utf-8"))
            invalid = (
                not profile.get("version")
                or INVALID_IN_A_PRESET & set(profile)
                or profile.get("inherits")
                or any(value == ["nil"] and key not in NULLABLE_KEYS for key, value in profile.items())
            )
            if invalid:
                raise RuntimeError("Local bundle contains an invalid preset")
            values = {key: serialized(value) for key, value in profile.items() if isinstance(value, list)}
            loaded[prefix + path.stem] = FakePreset(prefix + path.stem, path, values)
        selected = self.filaments.selected.name
        if selected.startswith(prefix) and selected not in loaded and self.filaments.edited.is_dirty:
            raise RuntimeError("A modified selected preset was removed or renamed")
        self.filaments.presets = {name: preset for name, preset in self.filaments.presets.items()
                                  if not name.startswith(prefix)} | loaded
        if selected in loaded:
            # The reload carries the unsaved edits across and rebases them, as the host does.
            before = self.filaments.selected.values
            edits = {key: value for key, value in self.filaments.edited.values.items() if value != before.get(key)}
            self.filaments.select(selected, edits)


class FakeFilamentDb:
    """Follows src/app/api/filaments/**/route.ts in Filament DB."""

    def __init__(self):
        self.filaments = {}
        self.calibrations = {}
        self.api_key = ""
        self.fail_with = None
        self.record_reads = 0
        self.posts = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def export(self, filament_id):
        record = self.filaments[filament_id]
        keys = {key: list(value) for key, value in record.get("settings", {}).items()}
        temperatures = record.get("temperatures", {})
        if "notes" in record and "filament_notes" not in keys:
            keys["filament_notes"] = [record["notes"]]
        fields = {"filament_type": record.get("type"), "filament_vendor": record.get("vendor"),
                  "filament_colour": record.get("color"), "filament_density": record.get("density"),
                  "filament_cost": record.get("cost"), "filament_diameter": record.get("diameter"),
                  "filament_max_volumetric_speed": record.get("maxVolumetricSpeed"),
                  "nozzle_temperature": temperatures.get("nozzle"),
                  "nozzle_temperature_initial_layer": temperatures.get("nozzleFirstLayer"),
                  "nozzle_temperature_range_low": temperatures.get("nozzleRangeMin"),
                  "nozzle_temperature_range_high": temperatures.get("nozzleRangeMax"),
                  "hot_plate_temp": temperatures.get("bed"),
                  "hot_plate_temp_initial_layer": temperatures.get("bedFirstLayer"),
                  "filament_shrinkage_compensation_z": record.get("shrinkageZ")}
        for row in record.get("bedTypeTemps", []):
            if row["bedType"] == "Hot Plate":
                fields["hot_plate_temp"] = row["temperature"]
        if record.get("shrinkageXY") is not None:
            fields["filament_shrink"] = f"{100 - record['shrinkageXY']}%"
        for key, source in fields.items():
            if source is not None:
                keys[key] = [str(source)]
        return {"name": record["name"], "type": "filament", "filament_id": f"fdb_{filament_id}",
                "from": "filament_db", "instantiation": "true", **keys}

    def handler(self):
        db = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, raw=False):
                data = body if raw else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def refused(self):
                if db.fail_with:
                    self.reply(*db.fail_with)
                    return True
                if db.api_key and self.headers.get("Authorization") != f"Bearer {db.api_key}":
                    self.reply(401, {"error": "Unauthorized"})
                    return True
                return False

            def do_GET(self):
                if self.refused():
                    return
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                record = re.fullmatch(r"/api/filaments/([^/]+)", url.path)
                if record and record[1] != "orcaslicer":
                    db.record_reads += 1
                    self.reply(200, db.filaments[record[1]])
                    return
                match = re.fullmatch(r"/api/filaments/([^/]+)/calibration", url.path)
                if match:
                    rows = db.calibrations.get((match[1], query["nozzle_diameter"][0]), [])
                    wanted = query.get("printer", [""])[0].strip().casefold()
                    # The real route only prioritizes a printer, and falls back to any row for the nozzle.
                    row = next((row for row in rows if (row.get("printer") or "").casefold() == wanted),
                               rows[0] if rows else None)
                    if row is None:
                        self.reply(404, {"error": "No calibration found"})
                    elif query.get("format") != ["orcaslicer"]:
                        self.reply(200, {"calibration": {}})
                    else:
                        self.reply(200, {"printer": row.get("printer"), "calibration_orca": row["keys"]})
                else:
                    ids = query["ids"][0].split(",") if "ids" in query else sorted(db.filaments)
                    self.reply(200, [db.export(filament_id) for filament_id in ids if filament_id in db.filaments])

            def do_POST(self):
                if self.refused():
                    return
                filament_id = re.fullmatch(r"/api/filaments/([^/]+)/orcaslicer", self.path)[1]
                if filament_id not in db.filaments:
                    self.reply(404, {"error": f"Filament not found: {filament_id}"})
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                db.posts.append(body)
                record = db.filaments[filament_id]
                for key, value in body.items():
                    if key == "temperatures":
                        record.setdefault("temperatures", {}).update(value)
                    elif key in ("type", "vendor", "color", "density", "cost", "diameter", "maxVolumetricSpeed"):
                        record[key] = value
                    else:
                        record.setdefault("settings", {})[key] = value
                self.reply(200, {"success": True})

        return Handler


def fake_orca():
    module = types.ModuleType("orca")
    module.ExecutionResult = ExecutionResult
    module.script = types.SimpleNamespace(ScriptPluginCapabilityBase=type("ScriptBase", (), {
        "get_config": lambda self: json.dumps(getattr(self, "config", {})),
    }))
    module.base = object
    module.plugin = lambda cls: cls
    module.registered = []
    module.register_capability = module.registered.append
    module.request_permissions = lambda **kwargs: setattr(module, "requested", kwargs)
    module.host = types.SimpleNamespace(
        ui=types.SimpleNamespace(NotificationLevel=NotificationLevel, notifications=[], dialogs=[], answer="ok"),
        preset=types.SimpleNamespace(),
    )
    module.host.ui.push_notification = lambda level, text: module.host.ui.notifications.append((level, text))

    def message(text, title, buttons, icon):
        module.host.ui.dialogs.append(text)
        return module.host.ui.answer

    module.host.ui.message = message
    return module


@pytest.fixture
def orca(tmp_path, monkeypatch):
    module = fake_orca()
    monkeypatch.setitem(sys.modules, "orca", module)
    return module


@pytest.fixture
def plugin(orca, tmp_path):
    """The plugin, installed in a data folder, as it would be on a machine with two users."""
    sys.modules.pop("orca_filament_db_sync_any", None)
    data_dir = tmp_path / "data"
    (data_dir / "system").mkdir(parents=True)
    (data_dir / "user" / "default").mkdir(parents=True)
    (data_dir / "user" / "cloud-user").mkdir()
    module = importlib.import_module("orca_filament_db_sync_any")
    module.__file__ = str(data_dir / "orca_plugins" / "orca_filament_db_sync_any" / "orca_filament_db_sync_any.py")
    module.data_dir_for_test = data_dir
    yield module
    sys.modules.pop("orca_filament_db_sync_any", None)


@pytest.fixture
def db():
    server = FakeFilamentDb()
    server.filaments[PLAIN_ID] = {
        "name": "Plain PLA", "type": "PLA", "vendor": "Spike", "color": "#112233", "density": 1.24,
        "temperatures": {"nozzle": 215, "bed": 60},
    }
    server.filaments[CALIBRATED_ID] = {
        "name": "Calibrated PLA", "type": "PLA", "vendor": "Spike", "temperatures": {"nozzle": 220},
    }
    server.calibrations[(CALIBRATED_ID, "0.4")] = [{"keys": {"filament_flow_ratio": ["0.97"], "pressure_advance": ["0.04"]}}]
    server.calibrations[(CALIBRATED_ID, "0.6")] = [{"keys": {"filament_flow_ratio": ["0.93"], "pressure_advance": ["0.02"]}}]
    yield server
    server.server.shutdown()


@pytest.fixture
def app(orca, plugin, db):
    """A running OrcaSlicer with the plugin loaded, its nozzle at 0.4 and an active user with a folder."""
    filaments = FakeFilaments()
    bundles = FakeBundles(plugin.data_dir_for_test, filaments, "cloud-user")
    state = SimpleNamespace(nozzle="0.4")
    bundle = SimpleNamespace(filaments=filaments, full_config_value=lambda key: state.nozzle,
                             current_filament_preset_names=lambda: [filaments.selected.name])
    orca.host.preset_bundle = lambda: bundle
    orca.host.preset.bundles = bundles
    pull_command = plugin.PullFromFilamentDb()
    pull_command.config = {"url": db.url}
    pull_command.on_load()
    return SimpleNamespace(
        filaments=filaments, bundles=bundles, state=state, db=db, plugin=plugin, orca=orca,
        pull=pull_command.execute, send=plugin.SendToFilamentDb().execute, config=pull_command.config,
        notifications=orca.host.ui.notifications,
        bundle_dir=plugin.data_dir_for_test / "user" / "cloud-user" / "_local" / BUNDLE_ID,
    )
