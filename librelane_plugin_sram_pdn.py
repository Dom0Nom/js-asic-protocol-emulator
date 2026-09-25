"""
LibreLane plugin: rewrite Metal4 PDN stripes onto the IHP SRAM's power columns.

LibreLane imports every module on the Python path whose name starts with
``librelane_plugin_`` (librelane/plugins.py).  ``python -m librelane`` and the
Tiny Tapeout GDS action both run from the repository root, so this file is
found without installing anything.  ``src/config.json`` inserts the step
after GeneratePDN:

    "meta": {
      "substituting_steps": {
        "+OpenROAD.GeneratePDN": "Project.ExtendPowerStripes"
      }
    }
"""
# Adapted from WilliamZhang20/protocol-emulator (Apache-2.0), which follows
# kdp1965/ihp-um-janestreet-prism. Change here: passes macro LEFs to the script.
import os
from glob import glob

from librelane.config import Variable
from librelane.steps import Step
from librelane.steps.odb import OdbpyStep

HERE = os.path.dirname(os.path.abspath(__file__))


@Step.factory.register()
class ExtendPowerStripes(OdbpyStep):
    id = "Project.ExtendPowerStripes"
    name = "Extend Power Stripes Over SRAM"

    config_vars = [
        Variable(
            "SRAM_PDN_MIN_PAIRS",
            int,
            "Minimum VPWR/VGND column pairs the SRAM gets in each of its "
            "regions (array L / band / array R).",
            default=2,
        ),
    ]

    def get_script_path(self):
        return os.path.join(HERE, "flow", "odb_sram_stripes.py")

    def get_command(self):
        return super().get_command() + [
            "--min-pairs", str(self.config["SRAM_PDN_MIN_PAIRS"]),
            "--grid-report-dir", self.step_dir,
        ] + [
            arg
            for macro in (self.config["MACROS"] or {}).values()
            for lef in macro.lef
            for arg in ("--lef", str(lef))
        ]

    def run(self, state_in, **kwargs):
        # Replace GeneratePDN's power grid violation counts (taken before the
        # SRAM supply columns were connected) with the counts for the final
        # grid, computed the same way OpenROAD.GeneratePDN computes them.
        from librelane.common import aggregate_metrics
        from librelane.steps.openroad import get_psm_error_count

        views_updates, metrics_updates = super().run(state_in, **kwargs)
        for report in glob(os.path.join(self.step_dir, "*-grid-errors.rpt")):
            net = os.path.basename(report).split("-", maxsplit=1)[0]
            with open(report, encoding="utf8") as f:
                count = get_psm_error_count(f)
            metrics_updates[f"design__power_grid_violation__count__net:{net}"] = count
        return views_updates, aggregate_metrics(
            metrics_updates,
            {"design__power_grid_violation__count": (0, lambda x: sum(x))},
        )


# --- netgen writes IHP SRAM power pin names (VDD!, VSS!, VDDARRAY!) into its
# LVS JSON with a stray backslash ("\VDD!"), which is not a valid JSON escape,
# and librelane.steps.netgen.LVS then dies in json.loads before the LVS
# checker runs.  Same monkeypatch as ihp-um-janestreet-prism.
import json as _json
import re as _re
import types as _types

import librelane.steps.netgen as _netgen

_BAD_ESCAPE = _re.compile(r'\\(\\|[^"\\/bfnrtu])')


def _repair_escapes(s):
    return _BAD_ESCAPE.sub(
        lambda m: '\\\\' if m.group(1) == '\\' else '\\\\' + m.group(1), s
    )


def _loads_repairing_escapes(s, *args, **kwargs):
    try:
        return _json.loads(s, *args, **kwargs)
    except _json.JSONDecodeError:
        return _json.loads(_repair_escapes(s), *args, **kwargs)


_netgen.json = _types.SimpleNamespace(
    loads=_loads_repairing_escapes,
    load=_json.load,
    dumps=_json.dumps,
    dump=_json.dump,
    JSONDecodeError=_json.JSONDecodeError,
)
