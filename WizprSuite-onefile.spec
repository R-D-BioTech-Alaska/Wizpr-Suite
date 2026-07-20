from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


root = Path(SPEC).resolve().parent

datas = [
    (str(root / "wizpr_suite" / "resources" / "theme_dark.qss"), "wizpr_suite/resources"),
    (str(root / "wizpr_suite" / "resources" / "theme_light.qss"), "wizpr_suite/resources"),
    (str(root / "wizpr_suite" / "resources" / "wizpr_suite_logo.png"), "wizpr_suite/resources"),
    (str(root / "wizpr_suite" / "resources" / "wizpr_ring_card.png"), "wizpr_suite/resources"),
]
binaries = []
hiddenimports = [
    "wizpr_suite.tools.local_transcribe_worker",
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.radios",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    "faster_whisper",
    "faster_whisper.audio",
    "faster_whisper.feature_extractor",
    "faster_whisper.tokenizer",
    "faster_whisper.transcribe",
    "faster_whisper.utils",
    "faster_whisper.vad",
    "ctranslate2",
    "ctranslate2._ext",
    "av",
    "tokenizers",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "numpy.core._exceptions",
    "numpy.core._multiarray_umath",
    "numpy.linalg._umath_linalg",
]

for package in ("ctranslate2", "av", "tokenizers", "onnxruntime"):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception:
        pass

for package in ("faster_whisper", "ctranslate2", "onnxruntime"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

for distribution in (
    "faster-whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "onnxruntime",
    "huggingface-hub",
    "openai",
):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

analysis = Analysis(
    [str(root / "wizpr_launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
        "tkinter",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtGraphsWidgets",
        "PySide6.QtHttpServer",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialBus",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtStateMachine",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="WizprSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "wizpr_suite.ico"),
    version=str(root / "assets" / "version_info.txt"),
    runtime_tmpdir=None,
)
