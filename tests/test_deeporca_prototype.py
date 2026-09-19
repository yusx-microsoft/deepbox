"""Offline UI checks for the standalone prototype, not the production renderer.

Run: python -m unittest discover -s tests -p test_deeporca_prototype.py -v
Uses already-installed Playwright/Chromium only; never downloads browsers.
"""
from pathlib import Path
import re
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


PROTOTYPE = Path(__file__).resolve().parents[1] / "web/prototypes/deeporca.html"


@unittest.skipIf(sync_playwright is None, "Local Playwright is not installed")
class DeepOrcaPrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Local Chromium unavailable (no install attempted): {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.network = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", lambda request: self.network.append(request.url)
                     if request.url.startswith(("http:", "https:", "ws:", "wss:")) else None)
        self.page.on("websocket", lambda socket: self.network.append(socket.url))
        self.context.route(re.compile(r"^(?:https?|wss?)://"), lambda route: route.abort())
        self.page.goto(PROTOTYPE.as_uri())

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], "JavaScript errors")
        self.assertEqual(self.network, [], "Prototype attempted a network request")

    def create(self, outcome="ready", name="Project assistant", project="deepbox"):
        self.page.locator("#add-agent").click()
        self.page.locator("#agent-name").fill(name)
        self.page.locator("#agent-project").select_option(project)
        self.page.locator("#create-outcome").select_option(outcome)
        self.page.get_by_role("button", name="Create agent", exact=True).click()
        self.page.locator("#messages .status-box code").filter(has_text="provisioning").wait_for()
        if outcome == "ready":
            self.page.locator("#demo-chat").wait_for()
        else:
            self.page.wait_for_function(
                "status => document.querySelector('#messages .status-box code')?.textContent === status",
                arg=outcome,
            )

    def finish_turn(self):
        self.page.wait_for_function("document.querySelector('.turn-status')?.textContent.startsWith('Completed')")

    def event_count(self):
        return int(self.page.locator("#event-count").inner_text())

    def test_01_initial_machine_and_creation_form(self):
        self.assertEqual(self.page.locator(".agent-button").count(), 0)
        self.assertIn("No agents yet", self.page.locator("#agent-tree").inner_text())
        self.assertTrue(self.page.locator("#new-conversation").is_disabled())
        self.page.locator("#add-agent").click()
        self.assertEqual(self.page.locator("#create-dialog input").count(), 1)
        self.assertEqual(self.page.locator("#create-dialog input[type=password]").count(), 0)
        self.assertIn("Create a new managed", self.page.locator("#create-dialog").inner_text())
        self.assertIn("Connector-default configuration template", self.page.locator("#create-dialog").inner_text())
        self.page.locator("#agent-name").fill("   ")
        self.page.get_by_role("button", name="Create agent", exact=True).click()
        self.assertEqual(self.page.locator(".agent-button").count(), 0)
        self.page.locator("#create-cancel").click()
        self.create()
        self.assertEqual(self.page.locator(".agent-button").count(), 1)
        self.assertEqual(self.page.locator(".session-button").count(), 1)
        self.assertEqual(self.page.locator(".user-message").count(), 0)
        self.assertIn("empty conversation", self.page.locator("#messages").inner_text().lower())
        self.assertIn("agent.created", self.page.locator("#lifecycle-events").inner_text())
        self.assertIn("agent.provisioning", self.page.locator("#lifecycle-events").inner_text())
        self.assertIn("agent.ready", self.page.locator("#lifecycle-events").inner_text())

    def test_02_failure_states_and_recovery(self):
        for outcome in ("needs_configuration", "error"):
            with self.subTest(outcome=outcome):
                before = self.page.locator(".session-button").count()
                self.create(outcome, name=outcome)
                self.assertEqual(self.page.locator(".session-button").count(), before)
                self.assertTrue(self.page.locator("#new-conversation").is_disabled())
                self.assertTrue(self.page.locator("#composer-area").is_hidden())
                self.page.locator("#agent-settings").click()
                self.page.locator("#retry-provision").click()
                self.page.locator("#demo-chat").wait_for()
                self.assertEqual(self.page.locator(".session-button").count(), before + 1)
        self.assertEqual(self.page.locator(".agent-button").count(), 2)

    def test_03_streaming_allowed_and_immediate_block(self):
        self.create()
        self.page.locator("#demo-chat").click()
        self.page.locator(".thinking").wait_for()
        self.page.locator(".thinking summary").click()
        self.page.locator(".tool-card.blocked").wait_for()
        self.assertTrue(self.page.locator("#stop").is_visible())
        self.assertTrue(self.page.locator("#send").is_disabled())
        self.page.locator(".tool-card.blocked summary").click()
        self.assertIn("review_not_supported", self.page.locator(".tool-card.blocked").inner_text())
        self.assertIn('"executed": false', self.page.locator(".tool-card.blocked").inner_text())
        self.assertIn("refused immediately", self.page.locator(".tool-card.blocked").inner_text())
        self.finish_turn()
        self.assertEqual(self.page.locator(".tool-card").count(), 2)
        self.assertTrue(self.page.locator(".thinking").evaluate("el => el.open"))
        self.assertTrue(self.page.locator(".tool-card.blocked").evaluate("el => el.open"))
        self.page.locator(".tool-card:not(.blocked) summary").click()
        self.assertIn('"final_verdict": "ALLOW"', self.page.locator(".tool-card:not(.blocked)").inner_text())
        self.assertEqual(self.page.get_by_role("button", name=re.compile(r"approve|allow once|allow always|confirm commands", re.I)).count(), 0)
        self.assertNotIn("approval.request", PROTOTYPE.read_text(encoding="utf-8"))
        self.assertNotIn("approval.response", PROTOTYPE.read_text(encoding="utf-8"))

    def test_04_stop_discards_delayed_output_and_escapes_input(self):
        self.create()
        self.page.locator("#prompt").fill('<img src=x onerror="window.UNSAFE=1">')
        self.page.locator("#send").click()
        self.page.locator(".tool-card").wait_for()
        self.page.locator("#stop").click()
        count = self.event_count()
        self.page.wait_for_timeout(1600)
        self.assertEqual(self.event_count(), count)
        self.assertIn("Stopped", self.page.locator(".turn-status").inner_text())
        self.assertIn("Cancelled", self.page.locator(".tool-card").inner_text())
        self.assertEqual(self.page.locator(".user-bubble img").count(), 0)
        self.assertIn("<img", self.page.locator(".user-bubble").inner_text())
        self.assertIsNone(self.page.evaluate("window.UNSAFE"))
        self.assertTrue(self.page.locator("#send").is_enabled())

    def test_05_offline_reconnect_and_replay_are_display_only(self):
        self.create()
        self.page.locator("#demo-chat").click()
        self.page.locator(".thinking").wait_for()
        self.page.locator("#connection-toggle").click()
        frozen = self.event_count()
        self.page.wait_for_timeout(5500)
        self.assertEqual(self.event_count(), frozen)
        self.assertTrue(self.page.locator("#send").is_disabled())
        self.assertTrue(self.page.locator("#add-agent").is_disabled())
        self.assertIn("Ready", self.page.locator(".agent-button").inner_text())
        self.page.locator("#connection-toggle").click()
        self.finish_turn()
        self.assertEqual(self.page.locator(".user-message").count(), 1)
        self.assertEqual(self.page.locator(".tool-card").count(), 2)
        count = self.event_count()
        self.page.locator("#replay-toggle").click()
        for control in ("#send", "#add-agent", "#new-conversation", "#agent-settings", "#connection-toggle"):
            self.assertTrue(self.page.locator(control).is_disabled(), control)
        self.page.locator("#replay-range").fill("0")
        self.assertEqual(self.page.locator(".user-message").count(), 0)
        self.page.locator("#replay-range").fill(str(count))
        self.assertEqual(self.page.locator(".tool-card").count(), 2)
        self.page.locator("#replay-toggle").click()
        self.assertEqual(self.event_count(), count)
        self.assertEqual(self.page.locator(".user-message").count(), 1)

    def test_06_agent_settings_and_independent_sessions(self):
        self.create()
        profile = self.page.locator("#binding-context .mono").inner_text()
        self.page.locator("#prompt").fill("First session")
        self.page.locator("#send").click()
        self.page.locator("#stop").click()
        first_count = self.event_count()
        self.page.locator("#new-conversation").click()
        self.assertEqual(self.page.locator(".agent-button").count(), 1)
        self.assertEqual(self.page.locator(".session-button").count(), 2)
        self.assertEqual(self.page.locator(".user-message").count(), 0)
        self.assertEqual(self.page.locator("#binding-context .mono").inner_text(), profile)
        self.page.locator("#agent-settings").click()
        self.page.locator("#settings-name").fill("Renamed assistant")
        self.page.get_by_role("button", name="Save display name").click()
        self.assertEqual(self.page.locator("#binding-context .mono").inner_text(), profile)
        self.page.locator(".session-button").first.click()
        self.assertEqual(self.event_count(), first_count)
        self.assertIn("First session", self.page.locator(".user-bubble").inner_text())
        self.create(name="Second Agent", project="design-system")
        self.assertNotEqual(self.page.locator("#binding-context .mono").inner_text(), profile)
        self.assertIn("design-system", self.page.locator("#breadcrumb").inner_text())

    def test_07_mobile_dialog_sidebar_and_inspector(self):
        for width in (390, 320):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 844})
                self.page.reload()
                self.page.locator("#menu-toggle").click()
                self.create(name="Narrow screen assistant")
                self.assertTrue(self.page.locator("#sidebar").is_hidden())
                self.page.locator("#agent-settings").click()
                self.assertTrue(self.page.locator("#settings-dialog").is_visible())
                self.assertTrue(self.page.evaluate("document.querySelector('#settings-dialog').scrollWidth <= document.querySelector('#settings-dialog').clientWidth"))
                self.page.locator("#settings-cancel").click()
                self.page.locator("#inspector-toggle").click()
                self.assertTrue(self.page.locator("#inspector").is_visible())
                self.page.locator("#inspector-close").click()
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                ids = self.page.locator("[id]").evaluate_all("els => els.map(e => e.id)")
                self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
