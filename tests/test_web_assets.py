from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_main_stylesheet_is_declared_once_in_html():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert index.count('href="/static/styles.css"') == 1
    assert "<style>" not in index
    assert "/static/styles.css" not in app
