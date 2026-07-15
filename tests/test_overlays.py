from scrapers.boss import dismiss_overlays


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        if self.selector == "div.overseas-nav-box":
            return int(self.page.overlay_visible)
        return int(self.page.overlay_visible and self.page.action_selector in self.selector)

    def is_visible(self):
        return self.count() > 0

    def evaluate(self, script):
        self.page.overlay_visible = False


class FakeKeyboard:
    def __init__(self, page): self.page = page
    def press(self, key):
        if self.page.escape_closes:
            self.page.overlay_visible = False


class FakePage:
    def __init__(self, *, visible=True, action_selector="never", escape_closes=False):
        self.overlay_visible = visible
        self.url = "https://www.zhipin.com/shanghai/?seoRefer=index"
        self.action_selector = action_selector
        self.escape_closes = escape_closes
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector): return FakeLocator(self, selector)
    def wait_for_timeout(self, timeout): pass


def test_dismiss_overlay_uses_css_and_preserves_same_page_and_url():
    page = FakePage()
    original_url = page.url
    original_id = id(page)
    result = dismiss_overlays(page)
    assert result == {"detected": True, "dismissed": True, "action": "css_hide"}
    assert id(page) == original_id
    assert page.url == original_url


def test_dismiss_overlay_reports_not_detected():
    assert dismiss_overlays(FakePage(visible=False))["detected"] is False


def test_dismiss_overlay_has_no_hard_url_assertion():
    import inspect
    assert 'assert "zhipin.com"' not in inspect.getsource(dismiss_overlays)
