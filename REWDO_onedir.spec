# -*- mode: python ; coding: utf-8 -*-
# REWDO v20.31 — Safety Hotfix / aplikacja okienkowa
#
# One executable:
#   REWDO_GUI.exe                  -> GUI
#   REWDO_GUI.exe --rewdo-cli ...  -> CLI helper
#
# Jeden plik wykonywalny zapobiega problemom wcześniejszej konstrukcji z dwoma plikami EXE.

from PyInstaller.utils.hooks import collect_data_files

datas = []
for name in [
    "USER_REFERENCE_90BW48.yaml",
    "USER_REFERENCE_102BW48.yaml",
    "USER_LISTENING_PROFILE.yaml",
    "README.md",
    "README_GUI_ONEDIR.md",
    "OPIS_TRYBOW.md",
    "CHANGELOG.md",
    "METRYKI_I_SKALY.md",
    "METRYKI_I_SKALY.csv",
    "90_BW48_main30.mdat",
    "USER_EMPIRICAL_CALIBRATION.json",
    "KALIBRACJA_PREDYKCJA_VS_POMIAR_v20_25a.md",
    "REW_API_INTEGRATION_v20_26.md",
    "VALIDATION_v20_26.md",
    "VALIDATION_v20_26a.md",
    "TIME_DOMAIN_ANALYSIS_v20_28.md",
    "VALIDATION_v20_28.md",
    "MDAT_PHASE_TRUTH_GUARD_v20_29.md",
    "VALIDATION_v20_29.md",
    "VALIDATION_v20_29b.md",
    "VALIDATION_v20_31.md",
    "AUDYT_NAPRAW_P0_v20_31.md",
    "KNOWN_LIMITATIONS_v20_31.md",
    "USER_REFERENCE_SLOT4MAX_90BW48.yaml",
]:
    datas.append((name, "."))

try:
    datas += collect_data_files("matplotlib", include_py_files=False)
except Exception:
    pass

hiddenimports = [
    "_tkinter",
    "tkinter",
    "tkinter.constants",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
    "numpy",
    "REWDO_GUI",
    "REWDO_CLI",
    "rew_common",
    "rew_mdat",
    "rew_crossover",
    "rew_scoring",
    "rew_reports",
    "rew_app_prefs",
    "rew_app_metadata",
    "rew_raw_measurement_check",
    "rew_crossover_ranking",
    "rew_post_dirac_ab",
    "rew_verify_user_slot",
    "rew_filter_generator",
    "rew_acoustic_crossover_designer",
    "rew_acoustic_biquad_designer",
    "rew_cut_only",
    "rew_metric_registry",
    "rew_empirical_calibration",
    "rew_prediction_measurement_calibrator",
    "rew_time_domain_analyzer",
    "rew_gain_bridge",
    "rew_slot4max_project",
    "rew_measurement_validation",
    "rew_measurement_source",
    "rew_gain_state",
    "rew_candidate_safety",
    "rew_api_client",
    "rew_api_gui",
    "REWDO_KALIBRATOR_GUI",
]

a = Analysis(
    ["REWDO_GUI_SINGLE_BOOT.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "IPython",
        "jupyter",
        "notebook",
        "scipy",
        "pandas",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="REWDO_GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="REWDO_GUI_ONEDIR",
)
