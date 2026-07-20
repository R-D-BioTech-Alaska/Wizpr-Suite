from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_pins_numpy_for_pyinstaller() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "numpy==1.26.4" in requirements
    assert '"numpy==1.26.4"' in project


def test_windows_spec_includes_numpy_runtime_modules() -> None:
    spec = (ROOT / "WizprSuite-onefile.spec").read_text(encoding="utf-8")
    assert '"numpy.core._exceptions"' in spec
    assert '"numpy.core._multiarray_umath"' in spec
    assert '"numpy.linalg._umath_linalg"' in spec


def test_primary_windows_builder_is_one_file_only() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "build_windows_exe.ps1").read_text(encoding="utf-8-sig")
    assert 'WizprSuite-onefile.spec' in script
    assert 'Join-Path $DistPath "WizprSuite.exe"' in script
    assert 'external runtime files' in script
    assert 'python311.dll' not in script
    assert 'Compress-Archive' not in script
    assert '$_.FullName -ne $Exe' in script


def test_onefile_spec_embeds_runtime_and_resources() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "WizprSuite-onefile.spec").read_text(encoding="utf-8")
    assert 'analysis.binaries' in spec
    assert 'analysis.datas' in spec
    assert 'exclude_binaries=True' not in spec
    assert 'runtime_tmpdir=None' in spec


def test_github_build_uploads_only_the_standalone_exe() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-windows-exe.yml").read_text(encoding="utf-8")
    assert "path: dist/WizprSuite.exe" in workflow
    assert "dist/WizprSuite/**" not in workflow
    assert "Wizpr-Suite-Windows-x64.zip" not in workflow


def test_windows_builder_uses_short_workspace_and_retries_access_violations() -> None:
    script = (ROOT / "build_windows_exe.ps1").read_text(encoding="utf-8-sig")
    assert 'Join-Path $env:LOCALAPPDATA "WizprSuiteBuild"' in script
    assert "$MaximumAttempts = 3" in script
    assert "-1073741819" in script
    assert "3221225477" in script
    assert "$BasePython" in script
    assert 'Join-Path $BasePython "DLLs"' in script


def test_windows_builder_pins_pre_adjacent_stub_analysis_toolchain() -> None:
    requirements = (ROOT / "build-requirements.txt").read_text(encoding="utf-8")
    verifier = (ROOT / "tools" / "verify_packager.py").read_text(encoding="utf-8")
    assert "pyinstaller==6.12.0" in requirements
    assert "pyinstaller-hooks-contrib==2025.2" in requirements
    assert 'expected_pyinstaller = "6.12.0"' in verifier
    assert 'expected_hooks = "2025.2"' in verifier

def test_windows_builder_wraps_single_dist_item_as_array() -> None:
    script = (ROOT / "build_windows_exe.ps1").read_text(encoding="utf-8-sig")
    assert "$FinalItems = @(Get-ChildItem -Path $FinalDist -Force)" in script
    assert "$FinalItems = Get-ChildItem -Path $FinalDist -Force" not in script



def test_windows_executable_uses_ws_icon_assets() -> None:
    spec = (ROOT / "WizprSuite-onefile.spec").read_text(encoding="utf-8")
    icon = ROOT / "assets" / "wizpr_suite.ico"
    logo = ROOT / "wizpr_suite" / "resources" / "wizpr_suite_logo.png"
    assert 'icon=str(root / "assets" / "wizpr_suite.ico")' in spec
    assert icon.stat().st_size > 10000
    assert logo.stat().st_size > 10000


def test_reference_layout_ring_art_is_bundled() -> None:
    spec = (ROOT / "WizprSuite-onefile.spec").read_text(encoding="utf-8")
    ring_art = ROOT / "wizpr_suite" / "resources" / "wizpr_ring_card.png"
    assert 'wizpr_ring_card.png' in spec
    assert ring_art.stat().st_size > 5000


def test_windows_executable_embeds_version_2_metadata() -> None:
    spec = (ROOT / "WizprSuite-onefile.spec").read_text(encoding="utf-8")
    version_file = ROOT / "assets" / "version_info.txt"
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version=str(root / "assets" / "version_info.txt")' in spec
    text = version_file.read_text(encoding="utf-8")
    assert "filevers=(2, 0, 2, 0)" in text
    assert "ProductVersion', '2.0.2" in text
    assert 'version = "2.0.2"' in project
