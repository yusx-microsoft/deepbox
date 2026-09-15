from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.styles = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.styles.append(values["href"])
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"])


def test_main_stylesheet_is_declared_once_in_html():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assets = Assets()
    assets.feed(index)
    assert sum(urlsplit(href).path == "/static/styles.css" for href in assets.styles) == 1
    assert "<style>" not in index
    assert "/static/styles.css" not in app


def test_boot_assets_exist_and_do_not_wait_on_remote_terminal_dependencies():
    assets = Assets()
    assets.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
    for address in assets.styles + assets.scripts:
        parsed = urlsplit(address)
        assert not parsed.netloc
        assert parsed.path.startswith("/static/")
        assert (ROOT / "web" / parsed.path.removeprefix("/static/")).is_file()
    paths = [urlsplit(address).path for address in assets.scripts]
    assert len(paths) == len(set(paths))
    assert paths.index("/static/pane.js") < paths.index("/static/workbench.js")
    assert paths[-1] == "/static/main.js"
