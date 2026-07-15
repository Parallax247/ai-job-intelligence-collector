from scrapers.boss import BossScraper


def test_manual_wait_has_no_page_operation_while_input_blocks(monkeypatch, capsys):
    calls = 0

    def fake_input(prompt):
        nonlocal calls
        calls += 1
        assert prompt == "请先使用指定命令启动Chrome并登录BOSS，然后按Enter继续。"
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    BossScraper._wait_for_cdp_chrome()
    assert calls == 1
