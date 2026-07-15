import hashlib
from pathlib import Path

from streamlit.testing.v1 import AppTest

from utils.keyword_library import KeywordLibrary, keyword_key


def _keyword_button_key(action: str, keyword: str) -> str:
    digest = hashlib.sha1(keyword_key(keyword).encode("utf-8")).hexdigest()[:12]
    return f"keyword_{action}_{digest}"


def test_streamlit_app_starts_without_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_SCANNER_KEYWORD_LIBRARY", str(tmp_path / "saved_keywords.json"))
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=15)
    assert not app.exception
    assert app.title[0].value == "AI Job Intelligence Collector"
    assert app.caption[0].value == "Collect job details, full-page screenshots, and structured Excel output"
    assert app.selectbox[0].options[0] == "BOSS Zhipin (available)"
    assert app.selectbox[0].options[1] == "Liepin (available)"
    assert app.selectbox[0].options[2] == "LinkedIn (not implemented)"
    assert app.button(key="start_scan").label == "Start collection"
    assert any(item.label == "Advanced settings and full logs" for item in app.expander)
    assert app.selectbox(key="advanced_save_mode").value == "snapshot"
    assert app.text_area(key="task_keywords_text").label == "Keywords for this run"
    assert any(
        item.value == "No saved keywords yet. Save the current list to reuse it later."
        for item in app.caption
    )


def test_frontend_uses_compact_layout_and_no_legacy_name():
    app_path = Path(__file__).parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    assert "BOSS岗位批量采集器" not in source
    assert "BOSS岗位采集器" not in source
    assert 'st.set_page_config(page_title="AI Job Intelligence Collector", layout="wide")' in source
    assert 'st.fragment(run_every=1.0)' in source
    assert 'with st.expander("Advanced settings and full logs", expanded=False)' in source
    assert 'type="primary"' in source
    assert 'task["keyword_completed"]' in source
    assert 'task["keyword_index"]}/{len(task_keywords)' not in source
    assert '"historical_skipped", "no_new_jobs"' in source
    assert 'state, icon = "Checked — no new jobs", "✓"' in source


def test_boss_unbound_disables_start_and_creates_no_result_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_SCANNER_OUTPUT_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("JOB_SCANNER_PID_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("JOB_SCANNER_KEYWORD_LIBRARY", str(tmp_path / "saved_keywords.json"))
    monkeypatch.setattr("utils.desktop_service.browser_status", lambda: {
        "chrome_running": False,
        "boss_found": False,
        "boss_url": "",
        "pages": [],
    })
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=15)
    assert not app.exception
    assert app.button(key="start_scan").disabled is True
    assert not (tmp_path / "results").exists()


def test_saved_keyword_chip_toggles_task_selection_without_deleting_library(
        tmp_path, monkeypatch):
    path = tmp_path / "saved_keywords.json"
    KeywordLibrary(path).save_keywords(["交易系统运维", "合规风控"])
    monkeypatch.setenv("JOB_SCANNER_KEYWORD_LIBRARY", str(path))
    monkeypatch.setenv("JOB_SCANNER_OUTPUT_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("JOB_SCANNER_PID_DIR", str(tmp_path / "pids"))
    monkeypatch.setattr("utils.desktop_service.browser_status", lambda: {
        "chrome_running": False,
        "boss_found": False,
        "boss_url": "",
        "pages": [],
    })
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=15)
    assert not app.exception

    toggle_key = _keyword_button_key("toggle", "交易系统运维")
    app.button(key=toggle_key).click().run(timeout=15)
    assert not app.exception
    assert app.text_area(key="task_keywords_text").value == "交易系统运维"

    app.button(key=toggle_key).click().run(timeout=15)
    assert not app.exception
    assert app.text_area(key="task_keywords_text").value == ""
    assert [row["name"] for row in KeywordLibrary(path).load()] == [
        "交易系统运维", "合规风控",
    ]


def test_delete_saved_keyword_keeps_current_task_text(tmp_path, monkeypatch):
    path = tmp_path / "saved_keywords.json"
    KeywordLibrary(path).save_keywords(["交易系统运维", "合规风控"])
    monkeypatch.setenv("JOB_SCANNER_KEYWORD_LIBRARY", str(path))
    monkeypatch.setattr("utils.desktop_service.browser_status", lambda: {
        "chrome_running": False,
        "boss_found": False,
        "boss_url": "",
        "pages": [],
    })
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=15)
    app.text_area(key="task_keywords_text").set_value("交易系统运维").run(timeout=15)
    app.button(key=_keyword_button_key("delete", "交易系统运维")).click().run(timeout=15)
    assert not app.exception
    assert app.text_area(key="task_keywords_text").value == "交易系统运维"
    assert [row["name"] for row in KeywordLibrary(path).load()] == ["合规风控"]
