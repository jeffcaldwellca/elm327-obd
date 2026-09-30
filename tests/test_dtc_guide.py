"""Trouble-code names, guidance data and severity estimates."""

import re
from importlib import resources

import pytest

from elm327obd import dtc
from elm327obd.dtc import (
    GENERIC,
    SEVERITIES,
    describe,
    expand_codes,
    guide,
    is_manufacturer_specific,
    name_source,
)
from elm327obd.pids import PIDS


# ---- hand-written guidance ------------------------------------------------------

def test_every_builtin_code_has_handwritten_guidance():
    missing = [c for c in GENERIC if guide(c).source != "guide"]
    assert missing == []


def test_guide_entries_are_complete_and_valid():
    for code in dtc._load_guide():
        g = guide(code)
        assert g.severity in SEVERITIES, code
        assert g.summary, f"{code} has no summary"
        assert g.causes, f"{code} has no likely causes"
        assert g.checks, f"{code} has no checks"
        assert all(p in PIDS for p in g.watch), code


def test_stop_list_is_small_and_intentional():
    """Guard against rules drifting into crying wolf: only these known codes are STOP."""
    known = set(GENERIC) | set(dtc._load_pyobd_codes())
    assert {c for c in known if guide(c).severity == "stop"} == {"P0217", "P0218", "P0298", "P0524"}


@pytest.mark.parametrize("code,severity", [
    ("P0301", "soon"), ("P0420", "low"), ("P0455", "low"), ("P0171", "soon"), ("P0524", "stop"),
    ("P0137", "low"), ("P0133", "monitor"), ("P0562", "soon"), ("U0151", "soon"), ("U0140", "monitor"),
])
def test_handwritten_severities(code, severity):
    assert guide(code).severity == severity


@pytest.mark.parametrize("spec,expected", [
    ("P0301-P0303", ["P0301", "P0302", "P0303"]),
    ("P0420", ["P0420"]),
    ("p0a0e-p0a10", ["P0A0E", "P0A0F", "P0A10"]),  # hex-aware
])
def test_expand_codes(spec, expected):
    assert expand_codes(spec) == expected


@pytest.mark.parametrize("spec", ["P0308-P0301", "P0301-U0301", "P0000-P0FFF"])
def test_expand_codes_rejects_bad_ranges(spec):
    with pytest.raises(ValueError):
        expand_codes(spec)


# ---- python-OBD names -------------------------------------------------------------

def test_python_obd_table_loaded_and_clean():
    table = dtc._load_pyobd_codes()
    assert len(table) == 2066
    assert all(re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", c) for c in table)
    assert not any("lntake" in v for v in table.values())  # OCR typo fixed on import
    data = resources.files("elm327obd") / "data"
    assert (data / "LICENSE.python-OBD").read_text().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "GNU General Public License" in (data / "python_obd_dtc.tsv").read_text().split("\n", 5)[3]


def test_name_precedence():
    # Curated built-in names win over python-OBD for the same code…
    assert name_source("P0420") == "built-in"
    assert describe("P0420") == GENERIC["P0420"]
    # …python-OBD fills in the long tail…
    assert name_source("P2A00") == "python-OBD"
    assert "O2 Sensor" in describe("P2A00")
    # …and anything else falls back to the code structure.
    assert name_source("P1450") == "category"


def test_user_csv_overrides_everything(isolated_config):
    path = isolated_config / "cfg" / "dtc.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("P0420,My own wording\nP1450,Ford: fuel tank pressure sensor\n")
    assert describe("P0420") == "My own wording"
    assert describe("P1450") == "Ford: fuel tank pressure sensor"
    assert name_source("P1450") == "custom"


# ---- estimates for codes without guidance -------------------------------------------

@pytest.mark.parametrize("code,severity", [
    ("P0218", "stop"),     # transmission fluid over temperature
    ("P0298", "stop"),     # engine oil over temperature
    ("P0522", "soon"),     # oil pressure *sensor* low voltage – not real low pressure
    ("P0031", "low"),      # O2 heater
    ("P3401", "monitor"),  # cylinder deactivation, not "emissions"
    ("U0402", "monitor"),  # invalid data from TCM: a comms issue first
    ("U0415", "soon"),     # invalid data from ABS: safety-related
    ("P1450", "monitor"),  # manufacturer-specific: unknown
    ("C0035", "soon"),     # chassis
])
def test_estimated_severities(code, severity):
    g = guide(code)
    assert g.source == "estimated"
    assert g.severity == severity


def test_every_known_code_gets_a_valid_guide():
    for code in dtc._load_pyobd_codes():
        g = guide(code)
        assert g.severity in SEVERITIES and g.summary, code


@pytest.mark.parametrize("code,oem", [
    ("P0420", False), ("P2A00", False), ("P1450", True), ("P3000", True), ("P3401", False),
    ("U0100", False), ("U1000", True), ("B1234", True), ("C0035", False), ("B3000", True),
])
def test_manufacturer_specific(code, oem):
    assert is_manufacturer_specific(code) is oem


# ---- user guide overrides ------------------------------------------------------------

def test_user_guide_overrides_and_extends(isolated_config):
    path = isolated_config / "cfg" / "dtc_guide.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('''
[[entry]]
codes = ["P0420"]
severity = "monitor"
summary = "My mechanic says watch it"
causes = ["Old converter"]
checks = ["Retest in spring"]

[[entry]]
codes = ["P1450"]
severity = "low"
summary = "Ford EVAP sensor"
''')
    assert guide("P0420").severity == "monitor"
    assert guide("P0420").source == "custom"
    assert guide("P1450").summary == "Ford EVAP sensor"
    assert guide("P0171").source == "guide"  # untouched built-ins remain


def test_broken_user_guide_is_reported_not_fatal(isolated_config):
    path = isolated_config / "cfg" / "dtc_guide.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[[entry]]\ncodes = ["P0420"]\nseverity = "panic"\n')
    assert guide("P0420").severity == "low"  # built-in still used
    assert dtc.GUIDE_ERRORS and "severity" in dtc.GUIDE_ERRORS[0]


# ---- CLI -------------------------------------------------------------------------------

def test_cli_explain(capsys):
    from elm327obd.cli import main

    main(["explain", "p0171", "U0415", "nope"])
    out = capsys.readouterr().out
    assert "P0171  [SOON]  System too lean (Bank 1)" in out
    assert "1. Vacuum leak" in out
    assert "U0415  [SOON – estimated]" in out
    assert "nope: not a trouble code" in out


def test_app_works_without_the_gpl_data_file(monkeypatch):
    """README promises the python-OBD file can be deleted to avoid GPL obligations."""
    class Missing:
        def __truediv__(self, _other):
            return self

        def read_text(self, **_kw):
            raise FileNotFoundError("python_obd_dtc.tsv")

    real_files = resources.files

    def files(pkg):
        root = real_files(pkg)

        class Root:
            def __truediv__(self, name):
                return Missing() if name == "data" else root / name

        return Root()

    monkeypatch.setattr(dtc.resources, "files", files)
    monkeypatch.setattr(dtc, "_pyobd_codes", None)
    assert dtc._load_pyobd_codes() == {}
    assert describe("P0420") == GENERIC["P0420"]  # built-ins still work
    assert name_source("P2A00") == "category"
    assert guide("P2A00").severity in SEVERITIES
