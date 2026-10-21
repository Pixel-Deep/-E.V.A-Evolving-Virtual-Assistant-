import unittest
import unittest.mock
from eva.core.task_router import TaskRouter
from eva.automation.desktop import route_and_execute


class DesktopAutomationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_direct_automation_routes(self):
        cases = [
            "open YouTube",
            "increase volume",
            "take a screenshot",
            "show battery level",
            "list running apps",
            "type hello world",
            "read clipboard",
            "show system info",
            "show network status",
            "show wifi status",
            "increase brightness",
            "set brightness to 60",
            "lock the computer",
            "check audio devices",
            "open Downloads folder",
            "open VS Code",
            "open Visual Studio Code",
            "open vscode",
            "test microphone",
            "test speakers",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_coding_automation_request_stays_coding(self):
        self.assertEqual(
            self.router.route("write a Python script to automate volume control").handler,
            "coding_engine",
        )

    def test_extended_actions_map_without_execution(self):
        # Parser routing is exercised by replacing execute with a test double.
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = {}
        try:
            def fake_execute(action, value=""):
                seen.update(action=action, value=value)
                return {"ok": True}
            desktop.execute = fake_execute
            cases = {
                "show system info": "system_info",
                "show network status": "network_info",
                "show wifi status": "wifi_status",
                "increase brightness": "brightness_up",
                "set brightness to 60": "brightness_set",
                "lock the computer": "lock_screen",
                "check audio devices": "audio_devices",
                "open Downloads folder": "app_open",
                "test microphone": "mic_test",
                "test speakers": "speaker_test",
            }
            for prompt, expected in cases.items():
                with self.subTest(prompt=prompt):
                    result = desktop.route_and_execute(prompt)
                    self.assertTrue(result["ok"])
                    self.assertEqual(seen["action"], expected)
                    if expected == "brightness_set":
                        self.assertEqual(seen["value"], "60")
        finally:
            desktop.execute = original

    def test_single_youtube_song_uses_pywhatkit_action(self):
        import eva.automation.desktop as desktop
        original = desktop._play_youtube
        seen = []
        try:
            desktop._play_youtube = lambda title: (seen.append(title) or {"ok": True, "action": "play_youtube"})
            result = desktop.route_and_execute("play baby on youtube")
            self.assertTrue(result["ok"])
            self.assertEqual(seen, ["baby"])
        finally:
            desktop._play_youtube = original

    def test_single_song_prompt_routes_to_desktop_automation(self):
        self.assertEqual(self.router.route("play baby on youtube").handler, "desktop_automation")

    def test_vscode_launch_variants_never_route_to_coding(self):
        for prompt in (
            "open VS Code", "open vs code", "open vscode", "open Visual Studio Code",
            "launch VS Code", "start VS Code editor", "run vscode",
        ):
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.handler, "desktop_automation")
                self.assertNotEqual(decision.handler, "coding_engine")

    def test_windows_browser_open_uses_explicit_new_tab(self):
        import eva.automation.desktop as desktop
        def which(name):
            return r"C:\Program Files\Google\Chrome\Application\chrome.exe" if name == "chrome.exe" else None
        tasklist = unittest.mock.Mock(returncode=0, stdout='"chrome.exe","1234"\n', stderr="")
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop.shutil, "which", side_effect=which), \
             unittest.mock.patch.object(desktop.subprocess, "run", return_value=tasklist), \
             unittest.mock.patch.object(desktop.subprocess, "Popen") as popen:
            result = desktop._open_browser_url("https://www.wikipedia.org")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "browser_new_tab")
        self.assertIn("--new-tab", popen.call_args.args[0])
        self.assertEqual(popen.call_args.args[0][-1], "https://www.wikipedia.org")

    def test_spotify_app_suffix_plays_through_normal_desktop_dispatch(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop, "_spotify_api_play", return_value=None), \
             unittest.mock.patch.object(desktop, "_launch_spotify_app_search", return_value={
                 "ok": True, "method": "spotify_app_search", "opened": "spotify:search:baby",
                 "playback_requested": False,
             }) as launch:
            result = desktop.route_and_execute("play baby on spotify app")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "spotify_app_search")
        launch.assert_called_once_with("baby")

    def test_spotify_desktop_ui_uses_quick_search_to_request_playback(self):
        import sys
        import types
        import eva.automation.desktop as desktop
        window = unittest.mock.MagicMock()
        fake_gui = types.SimpleNamespace(
            getWindowsWithTitle=unittest.mock.Mock(return_value=[window]),
            hotkey=unittest.mock.Mock(),
            write=unittest.mock.Mock(),
            press=unittest.mock.Mock(),
        )
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop.time, "sleep"), \
             unittest.mock.patch.dict(sys.modules, {"pyautogui": fake_gui}):
            result = desktop._spotify_desktop_ui_play("Baby Shark")
        self.assertTrue(result["ok"])
        self.assertTrue(result["playback_requested"])
        self.assertFalse(result["playback_confirmed"])
        window.activate.assert_called_once()
        fake_gui.hotkey.assert_called_once_with("ctrl", "k")
        fake_gui.write.assert_called_once_with("Baby Shark", interval=0.025)
        self.assertEqual([call.args[0] for call in fake_gui.press.call_args_list], ["enter", "tab", "enter", "space"])

    def test_spotify_desktop_is_attempted_before_api_playback(self):
        import eva.automation.desktop as desktop
        order = []
        def app_search(song):
            order.append(("app", song))
            return {"ok": True, "method": "spotify_app_search", "opened": "spotify:search:baby"}
        def api_play(song):
            order.append(("api", song))
            return {"ok": True, "action": "play_spotify", "method": "spotify_web_api", "query": song,
                    "playback_requested": True, "spotify_uri": "spotify:track:123"}
        with unittest.mock.patch.object(desktop, "_launch_spotify_app_search", side_effect=app_search), \
             unittest.mock.patch.object(desktop, "_spotify_api_play", side_effect=api_play):
            result = desktop.route_and_execute("play baby on spotify")
        self.assertTrue(result["ok"])
        self.assertTrue(result["playback_requested"])
        self.assertEqual(order, [("app", "baby"), ("api", "baby")])
        self.assertEqual(result["app_launch"]["method"], "spotify_app_search")

    def test_spotify_app_absent_falls_back_to_spotify_web(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop, "_spotify_api_play", return_value=None), \
             unittest.mock.patch.object(desktop, "_launch_spotify_app_search", return_value={
                 "ok": False, "not_found": True, "error": "Spotify desktop app was not found."
             }), \
             unittest.mock.patch.object(desktop, "_open_browser_url", return_value={
                 "ok": True, "method": "browser", "opened": "https://open.spotify.com/search/baby"
             }) as browser:
            result = desktop.route_and_execute("play baby on spotify")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "browser_search_fallback")
        browser.assert_called_once_with("https://open.spotify.com/search/baby")

    def test_spotify_song_uses_app_search_before_browser_fallback(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop, "_spotify_api_play", return_value=None), \
             unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_ps", return_value={
                 "ok": True, "method": "spotify_app_search", "opened": "spotify:search:baby"
             }) as ps:
            result = desktop.route_and_execute("play baby in spotify")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "spotify_app_search")
        self.assertIn("spotify:search:baby", ps.call_args.args[0])
        self.assertFalse(result["playback_requested"])

    def test_spotify_song_falls_back_to_web_when_app_uri_is_unavailable(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop, "_spotify_api_play", return_value=None), \
             unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_ps", return_value={"ok": False, "not_found": True}), \
             unittest.mock.patch.object(desktop, "_open_browser_url", return_value={
                 "ok": True, "method": "browser", "opened": "https://open.spotify.com/search/baby"
             }) as browser:
            result = desktop.route_and_execute("play baby on spotify")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "browser_search_fallback")
        self.assertEqual(browser.call_args.args[0], "https://open.spotify.com/search/baby")

    def test_open_vs_code_routes_to_desktop_not_coding_agent(self):
        self.assertEqual(self.router.route("open VS Code").handler, "desktop_automation")
        self.assertEqual(self.router.route("open visual studio code").handler, "desktop_automation")
        self.assertEqual(self.router.route("open vscode").handler, "desktop_automation")
        self.assertEqual(self.router.route("write code for a VS Code extension").handler, "coding_engine")

    def test_spotify_song_and_open_site_runs_both_actions(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("play baby in spotify and open github")
            self.assertTrue(result["ok"])
            self.assertEqual(seen, [("play_spotify", "baby"), ("web", "github")])
            self.assertEqual(len(result["results"]), 2)
        finally:
            desktop.execute = original

    def test_vscode_missing_uses_vscode_dev_fallback(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_try_open_native_app", return_value=None), \
             unittest.mock.patch.object(desktop, "_open_browser_url", return_value={
                 "ok": True, "method": "browser", "opened": "https://vscode.dev"
             }) as browser:
            result = desktop.route_and_execute("open VS Code")
        self.assertTrue(result["ok"])
        self.assertEqual(browser.call_args.args[0], "https://vscode.dev")

    def test_vscode_native_launch_includes_standard_windows_paths(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_ps", return_value={
                 "ok": True, "opened": "Visual Studio Code", "method": "installed_app"
             }) as ps:
            result = desktop._try_open_native_app("visual studio code")
        self.assertTrue(result["ok"])
        script = ps.call_args.args[0]
        self.assertIn("Programs\\Microsoft VS Code\\Code.exe", script)
        self.assertIn("ProgramFiles(x86)", script)

    def test_spotify_api_uses_configured_user_access_token(self):
        import eva.automation.desktop as desktop
        class FakeResponse:
            def __init__(self, payload=None, status=204):
                self.payload = payload or {}
                self.status = status
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return __import__("json").dumps(self.payload).encode()
        responses = [
            FakeResponse({"tracks": {"items": [{"name": "Baby", "artists": [{"name": "Justin Bieber"}], "uri": "spotify:track:123"}]}}),
            FakeResponse({}, 204),
        ]
        with unittest.mock.patch.dict("os.environ", {"SPOTIFY_ACCESS_TOKEN": "test-token"}), \
             unittest.mock.patch.object(desktop, "urlopen", side_effect=responses) as urlopen_mock:
            result = desktop._spotify_api_play("Baby")
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "spotify_web_api")
        self.assertTrue(result["playback_requested"])
        self.assertEqual(result["spotify_uri"], "spotify:track:123")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_common_apps_have_browser_fallback_urls(self):
        import eva.automation.desktop as desktop
        aliases = desktop._website_aliases()
        self.assertEqual(aliases["whatsapp"], "https://web.whatsapp.com")
        self.assertEqual(aliases["spotify"], "https://open.spotify.com")
        self.assertIn("discord", aliases)

    def test_web_alias_checks_native_app_before_browser_fallback(self):
        import eva.automation.desktop as desktop
        original_native = desktop._try_open_native_app
        original_windows = desktop._windows
        seen = []
        try:
            desktop._try_open_native_app = lambda target: (seen.append(("native", target)) or {"ok": True, "opened": "Spotify", "method": "installed_app"})
            desktop._windows = lambda action, value="": (seen.append((action, value)) or {"ok": True})
            result = desktop.execute("web", "spotify")
            self.assertTrue(result["ok"])
            self.assertEqual(seen, [("native", "spotify")])
        finally:
            desktop._try_open_native_app = original_native
            desktop._windows = original_windows

    def test_unsupported_request_does_not_run_shell(self):
        result = route_and_execute("run arbitrary powershell command")
        self.assertFalse(result["ok"])
        self.assertIn("couldn't map", result["error"].lower())

    def test_action_parser_is_deterministic(self):
        # This tests routing only; platform actions are not executed here.
        from eva.core.task_router import _looks_like_desktop_automation
        self.assertTrue(_looks_like_desktop_automation("open YouTube"))
        self.assertFalse(_looks_like_desktop_automation("explain how desktop automation works"))


    def test_multi_website_opening_dispatches_each_target(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("open Spotify, GitHub and YouTube")
            self.assertTrue(result["ok"])
            self.assertEqual([x[1] for x in seen], ["spotify", "github", "youtube"])
            self.assertEqual(len(result["results"]), 3)
        finally:
            desktop.execute = original

    def test_song_searches_multiple_services(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("play Baby on YouTube and Spotify")
            self.assertTrue(result["ok"])
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], ("play_youtube", "Baby"))
            self.assertEqual(seen[1], ("play_spotify", "Baby"))
            self.assertEqual([item["action"] for item in result["results"]], ["play_youtube", "play_spotify"])
        finally:
            desktop.execute = original

    def test_mixed_song_search_and_open_site_executes_every_step(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            prompt = "play baby on youtube and open github"
            self.assertEqual(self.router.route(prompt).handler, "desktop_automation")
            result = desktop.route_and_execute(prompt)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["results"]), 2)
            self.assertEqual(seen[0], ("play_youtube", "baby"))
            self.assertEqual(seen[1], ("web", "github"))
        finally:
            desktop.execute = original

    def test_yotube_typo_routes_and_opens_all_requested_sites(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            prompt = "open yotube spotify github"
            self.assertEqual(self.router.route(prompt).handler, "desktop_automation")
            result = desktop.route_and_execute(prompt)
            self.assertTrue(result["ok"])
            self.assertEqual([value for action, value in seen], ["youtube", "spotify", "github"])
            self.assertEqual(len(result["results"]), 3)
        finally:
            desktop.execute = original

    def test_desktop_automation_literal_is_diagnostic_not_chat(self):
        from eva.core.task_router import _looks_like_desktop_automation
        self.assertTrue(_looks_like_desktop_automation("desktop_automation"))

    def test_aliases_and_missing_commas_open_all_sites(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            prompt = "open insta facebook and whatsap"
            self.assertEqual(self.router.route(prompt).handler, "desktop_automation")
            result = desktop.route_and_execute(prompt)
            self.assertTrue(result["ok"])
            self.assertEqual([value for action, value in seen], ["instagram", "facebook", "whatsapp"])
            self.assertEqual(len(result["results"]), 3)
        finally:
            desktop.execute = original

    def test_then_chained_commands(self):
        import eva.automation.desktop as desktop
        original = desktop.route_and_execute
        calls = []
        try:
            # Intercept recursive single-clause execution only.
            def fake(text):
                calls.append(text)
                if text.lower().startswith("open "):
                    return {"ok": True, "action": "web"}
                return {"ok": True, "action": "volume_up"}
            desktop.route_and_execute = fake
            # Exercise the actual multi-parser directly to avoid self-recursion.
            result = desktop._multi_action("open YouTube then increase volume")
            self.assertTrue(result["ok"])
            self.assertEqual(calls, ["open YouTube", "increase volume"])
        finally:
            desktop.route_and_execute = original


    def test_windows_native_app_uses_explorer_apps_folder_launch(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_ps", return_value={
                 "ok": True, "opened": "Spotify", "method": "installed_app", "launch_requested": True
             }) as ps:
            result = desktop._try_open_native_app("spotify")
        self.assertTrue(result["ok"])
        script = ps.call_args.args[0]
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn("Start-Process -FilePath $explorer", script)
        self.assertIn("shell:AppsFolder", script)
        self.assertIn("-ErrorAction Stop", script)

    def test_native_launch_failure_falls_back_to_browser(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_try_open_native_app", return_value={
                 "ok": False, "method": "installed_app", "error": "app launch failed"
             }), \
             unittest.mock.patch.object(desktop, "_open_browser_url", return_value={
                 "ok": True, "method": "browser", "opened": "https://open.spotify.com"
             }) as browser:
            result = desktop.execute("web", "spotify")
        self.assertTrue(result["ok"])
        self.assertEqual(browser.call_args.args[0], "https://open.spotify.com")
        self.assertIn("native_app_warning", result)

    def test_multiple_websites_request_a_separate_browser_launch_each(self):
        import eva.automation.desktop as desktop
        opened = []
        def fake_open(url):
            opened.append(url)
            return {"ok": True, "opened": url, "method": "browser", "launch_requested": True}
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_try_open_native_app", return_value=None), \
             unittest.mock.patch.object(desktop, "_open_browser_url", side_effect=fake_open):
            result = desktop.route_and_execute("open insta facebook and whatsap")
        self.assertTrue(result["ok"])
        self.assertEqual(opened, [
            "https://www.instagram.com", "https://www.facebook.com", "https://web.whatsapp.com"
        ])
        self.assertEqual(len(result["results"]), 3)

    def test_and_then_clause_split_does_not_leave_conjunction_attached(self):
        import eva.automation.desktop as desktop
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True})
            result = desktop.route_and_execute("open Spotify and then open GitHub")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["results"]), 2)
        finally:
            desktop.execute = original


    def test_browser_helper_requests_a_new_tab(self):
        import eva.automation.desktop as desktop
        url = "https://www.instagram.com"
        with unittest.mock.patch.object(desktop.webbrowser, "open", return_value=True) as browser:
            result = desktop._open_browser_url(url)
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "browser")
        browser.assert_called_once_with(url, new=2, autoraise=True)

    def test_multi_site_request_reaches_real_browser_fallback_for_each_target(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_try_open_native_app", return_value=None), \
             unittest.mock.patch.object(desktop.webbrowser, "open", return_value=True) as browser:
            result = desktop.route_and_execute("open insta facebook and whatsap")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(
            [call.args[0] for call in browser.call_args_list],
            ["https://www.instagram.com", "https://www.facebook.com", "https://web.whatsapp.com"],
        )

    def test_app_open_known_web_app_uses_browser_when_native_app_is_missing(self):
        import eva.automation.desktop as desktop
        with unittest.mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             unittest.mock.patch.object(desktop, "_try_open_native_app", return_value=None), \
             unittest.mock.patch.object(desktop.webbrowser, "open", return_value=True) as browser:
            result = desktop.execute("app_open", "spotify")
        self.assertTrue(result["ok"])
        browser.assert_called_once_with("https://open.spotify.com", new=2, autoraise=True)


if __name__ == "__main__":
    unittest.main()

class MixedAppWebsiteOpeningTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_mixed_apps_and_websites_dispatch_in_requested_order(self):
        import eva.automation.desktop as desktop
        seen = []
        original = desktop.execute
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("open notepad wikipedia camera google drive")
        finally:
            desktop.execute = original
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [
            ("app_open", "notepad"),
            ("web", "wikipedia"),
            ("app_open", "camera"),
            ("web", "google drive"),
        ])
        self.assertEqual([item["target"] for item in result["results"]], [
            "notepad", "wikipedia", "camera", "google drive"
        ])

    def test_mixed_apps_and_websites_routes_to_desktop_automation(self):
        self.assertEqual(self.router.route("open notepad wikipedia camera google drive").handler,
                         "desktop_automation")
