"""A submission must contain runnable firmware, tools and verification."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run(*args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.fixture
def source(tmp_path):
    # Exercise the real exporter in a disposable repository. Neither the
    # user's checkout nor a public branch is modified by this regression.
    src = tmp_path / "source"
    src.mkdir()
    files = run("git", "ls-files", "-z", cwd=ROOT).stdout.split("\0")
    for name in files:
        if name and (ROOT / name).is_file():
            target = src / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
    run("git", "init", "-q", cwd=src)
    run("git", "add", ".", cwd=src)
    run("git", "-c", "user.name=Export test", "-c",
        "user.email=export@example.invalid", "commit", "-qm", "Fixture", cwd=src)
    return src


@pytest.mark.parametrize("precreated", [False, True])
def test_public_export_is_runnable(source, tmp_path, precreated):
    dst = tmp_path / "export"
    if precreated:
        run("git", "worktree", "add", "-qb", "export", str(dst), cwd=source)
    run("bash", "tools/export_public.sh", str(dst), cwd=source)
    required = ["README.md", "fw/reset.s", "fw/uart.s", "fw/tempo.inc",
                "tools/tempo/asm.py", "tools/tempo/sim.py", "tools/gen_images.py",
                "host/tempo_host/images.py", "host/tempo_host/uart.py",
                "test/Makefile", "test/test_usecases.py", "test/lockstep.py",
                "test/sram/RM_IHPSG13_1P_1024x16_c2_bm_bist.v",
                "formal/core.sby", "formal/lane_miter.sv",
                "docs/architecture.md", "docs/verification.md", "docs/use-cases.md",
                "docs/benchmarks.md",
                "tools/bench_rates.py", "tools/tests/test_transport.py",
                "fw/demo_multibus.s", "fw/manchester.s", "fw/manchester_core.inc",
                "tools/tempo/models/linecode.py", "tools/tempo/demo_checks.py",
                "tools/tests/test_demos.py",
                "notes/area.md", "notes/bugs.md", ".github/workflows/gds.yaml",
                ".github/workflows/test.yaml", "src/project.v"]
    for name in required:
        assert (dst / name).is_file(), "Export omits " + name
        assert (dst / name).read_bytes() == (source / name).read_bytes(), name
    # Assemble every image from the exported source and run independent ISA
    # tests in a fresh process whose cwd/import path is the exported checkout.
    run(sys.executable, "tools/gen_images.py", "--check", cwd=dst)
    run(sys.executable, "-m", "pytest", "-q", "tests/test_asm.py",
        "tests/test_isa.py", cwd=dst / "tools")
    # The host library must also import without reaching the private tree.
    code = "from tempo_host.uart import Uart; from tempo_host.i2c import I2c"
    env = dict(os.environ, PYTHONPATH=str(dst / "host"))
    subprocess.run([sys.executable, "-c", code], cwd=dst, env=env, check=True)


@pytest.mark.parametrize("target", ["source", "unrelated"])
def test_export_rejects_checkout_that_it_does_not_own(source, tmp_path, target):
    dst = source if target == "source" else tmp_path / "unrelated"
    if target == "unrelated":
        dst.mkdir()
        run("git", "init", "-q", cwd=dst)
    before = (source / "README.md").read_bytes()
    result = run("bash", "tools/export_public.sh", str(dst), cwd=source, check=False)
    assert result.returncode != 0, result.stdout
    assert (source / "README.md").read_bytes() == before


def test_export_preserves_unreviewed_destination_changes(source, tmp_path):
    dst = tmp_path / "export"
    run("git", "worktree", "add", "-qb", "export", str(dst), cwd=source)
    (dst / "README.md").write_text("Unreviewed local edits\n")
    result = run("bash", "tools/export_public.sh", str(dst), cwd=source, check=False)
    assert result.returncode != 0, result.stdout
    assert (dst / "README.md").read_text() == "Unreviewed local edits\n"
