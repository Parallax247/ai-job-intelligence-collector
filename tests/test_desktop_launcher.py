from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_launcher_uses_one_dedicated_chrome_and_cdp_tabs():
    source = (ROOT / "scripts" / "start_job_collector.sh").read_text(encoding="utf-8")
    assert "--server.headless true" in source
    assert "/json/new?" in source
    assert "/json/activate/" in source
    assert "JOB_SCANNER_OUTPUT_ROOT" in source
    assert "AI Job Intelligence Collector" in source
    assert 'SCRIPT_DIR="${0:A:h}"' in source
    assert 'PROJECT_DIR="${SCRIPT_DIR:h}"' in source
    assert "/usr/bin/open" not in source
    assert 'wait "$STREAMLIT_PID"' not in source
    assert "about:blank" not in source
    assert ".launcher.lock" in source
    assert "nohup python -m streamlit run app.py" in source


def test_applescript_app_launcher_never_opens_terminal():
    source = (ROOT / "scripts" / "JobCollectorLauncher.applescript").read_text(encoding="utf-8")
    assert "__LAUNCHER_SCRIPT__" in source
    assert "nohup" in source
    assert "launcher.log" in source
    assert 'tell application "Terminal"' not in source
    assert ".command" not in source


def test_repeated_launch_is_guarded_before_starting_streamlit():
    source = (ROOT / "scripts" / "start_job_collector.sh").read_text(encoding="utf-8")
    lock_position = source.index('mkdir "$LAUNCH_LOCK"')
    streamlit_position = source.index("python -m streamlit run app.py")
    assert lock_position < streamlit_position
    assert 'if ! pid_alive "$STREAMLIT_PID"' in source


def test_installer_resolves_project_path_and_builds_app():
    source = (ROOT / "scripts" / "install_macos_app.sh").read_text(encoding="utf-8")
    assert 'APP_PATH="$DESKTOP_HOME/AI Job Intelligence Collector.app"' in source
    assert 'PROJECT_DIR="${SCRIPT_DIR:h}"' in source
    assert "__LAUNCHER_SCRIPT__" in (ROOT / "scripts" / "JobCollectorLauncher.applescript").read_text(encoding="utf-8")
    assert "/usr/bin/osacompile" in source


def test_stop_script_is_portable_and_has_legacy_alias():
    stop_source = (ROOT / "scripts" / "stop_job_collector.sh").read_text(encoding="utf-8")
    alias_source = (ROOT / "scripts" / "stop_boss_scanner.sh").read_text(encoding="utf-8")
    assert 'SCRIPT_DIR="${0:A:h}"' in stop_source
    assert ".ai-job-collector-chrome" in stop_source
    assert "stop_job_collector.sh" in alias_source
