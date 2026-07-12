from pathlib import Path
from unittest.mock import patch

from src.notify import _collapse_whitespace, _escape_applescript_string, send_macos_notification

# The applet backend (preferred when built -- see src/notify.py's module
# docstring) is checked before terminal-notifier/osascript, so every test
# of those two backends pins APPLET_PATH to a path that can't exist. Without
# this, the terminal-notifier/osascript tests would silently test the applet
# path instead on any machine where the real applet has been built.
NO_APPLET = Path("/nonexistent/AutonomousTraderNotifier.app")


class TestEscapeApplescriptString:
    def test_escapes_double_quotes(self):
        assert _escape_applescript_string('He said "hi"') == 'He said \\"hi\\"'

    def test_escapes_backslashes(self):
        assert _escape_applescript_string("a\\b") == "a\\\\b"

    def test_escapes_backslash_before_quote_in_correct_order(self):
        # Backslashes must be escaped BEFORE quotes, or a message ending in
        # a literal backslash right before a quote would produce an
        # unescaped quote in the output (the quote's own escaping backslash
        # would get double-escaped instead of the original one).
        result = _escape_applescript_string('\\"')  # input: one backslash, one quote
        assert result == "\\" * 3 + '"'  # expected: three backslashes, one quote

    def test_collapses_newlines_and_extra_whitespace(self):
        assert _escape_applescript_string("line1\nline2   line3") == "line1 line2 line3"

    def test_leaves_plain_text_unchanged(self):
        assert _escape_applescript_string("AAPL buy x10") == "AAPL buy x10"


@patch("src.notify.APPLET_PATH", NO_APPLET)
class TestSendMacosNotificationViaTerminalNotifier:
    """terminal-notifier is preferred whenever it's installed (see
    src/notify.py's module docstring for why) -- these tests simulate that
    by patching shutil.which to report it as present.
    """

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier")
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_calls_terminal_notifier_with_expected_args(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message")
        args = mock_run.call_args[0][0]
        assert args[0] == "/opt/homebrew/bin/terminal-notifier"
        assert args[1:5] == ["-title", "Title", "-message", "Message"]

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier")
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_includes_subtitle_flag_when_given(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message", subtitle="Sub")
        args = mock_run.call_args[0][0]
        assert "-subtitle" in args
        assert args[args.index("-subtitle") + 1] == "Sub"

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier")
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_does_not_escape_quotes_since_no_shell_is_involved(self, mock_platform, mock_which, mock_run):
        # terminal-notifier's args are passed as separate argv elements, not
        # embedded in a parsed script string -- a literal quote should pass
        # through completely unchanged, unlike the osascript path below.
        send_macos_notification("Title", 'AAPL: reasoning says "bullish"')
        args = mock_run.call_args[0][0]
        assert args[4] == 'AAPL: reasoning says "bullish"'


@patch("src.notify.TERMINAL_NOTIFIER_FALLBACK_PATHS", ())
@patch("src.notify.APPLET_PATH", NO_APPLET)
class TestSendMacosNotificationViaOsascriptFallback:
    """Falls back to osascript only when terminal-notifier isn't installed."""

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_calls_osascript_with_expected_script(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message")
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"
        assert args[1] == "-e"
        assert args[2] == 'display notification "Message" with title "Title"'

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_escapes_message_containing_quotes(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", 'AAPL: reasoning says "bullish"')
        script = mock_run.call_args[0][0][2]
        assert '\\"bullish\\"' in script

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_includes_subtitle_when_given(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message", subtitle="Sub")
        script = mock_run.call_args[0][0][2]
        assert 'subtitle "Sub"' in script

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_omits_subtitle_clause_when_not_given(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message")
        script = mock_run.call_args[0][0][2]
        assert "subtitle" not in script


@patch("src.notify.TERMINAL_NOTIFIER_FALLBACK_PATHS", ())
@patch("src.notify.APPLET_PATH", NO_APPLET)
class TestSendMacosNotificationGeneral:
    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Linux")
    def test_noop_on_non_macos(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message")
        mock_run.assert_not_called()

    @patch("src.notify.subprocess.run", side_effect=RuntimeError("boom"))
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_swallows_subprocess_failures_without_raising(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "Message")  # must not raise

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_truncates_long_messages(self, mock_platform, mock_which, mock_run):
        send_macos_notification("Title", "x" * 1000, max_message_length=50)
        script = mock_run.call_args[0][0][2]
        assert script.count("x") == 50


@patch("src.notify.TERMINAL_NOTIFIER_FALLBACK_PATHS", ())
class TestSendMacosNotificationViaApplet:
    """The compiled-applet backend is used when terminal-notifier isn't
    installed but the applet exists (built by
    scripts/setup_notifier_applet.sh) -- payload goes through a file since
    applets can't take argv, and the applet is launched with `open -g -W`
    (background, wait-for-exit). shutil.which is pinned to None in every
    test here so they exercise the applet path even on a machine that has
    terminal-notifier installed."""

    def _send(self, tmp_path, *args, **kwargs):
        applet = tmp_path / "AutonomousTraderNotifier.app"
        applet.mkdir()
        payload = tmp_path / "notification.txt"
        with (
            patch("src.notify.platform.system", return_value="Darwin"),
            patch("src.notify.shutil.which", return_value=None),
            patch("src.notify.subprocess.run") as mock_run,
            patch("src.notify.APPLET_PATH", applet),
            patch("src.notify.APPLET_PAYLOAD_PATH", payload),
        ):
            send_macos_notification(*args, **kwargs)
        return applet, payload, mock_run

    def test_writes_payload_file_and_launches_applet_in_background(self, tmp_path):
        applet, payload, mock_run = self._send(tmp_path, "Title", "Message", subtitle="Sub")
        assert payload.read_text(encoding="utf-8") == "Title\nMessage\nSub"
        assert mock_run.call_args[0][0] == ["open", "-g", "-W", str(applet)]

    def test_payload_third_line_empty_when_no_subtitle(self, tmp_path):
        _, payload, _ = self._send(tmp_path, "Title", "Message")
        assert payload.read_text(encoding="utf-8") == "Title\nMessage\n"

    def test_collapses_newlines_in_fields_to_protect_line_based_payload(self, tmp_path):
        # A newline inside any field would shift every following line of the
        # payload file, corrupting the field mapping the applet relies on.
        _, payload, _ = self._send(tmp_path, "Title", "line1\nline2")
        assert payload.read_text(encoding="utf-8") == "Title\nline1 line2\n"

    def test_terminal_notifier_preferred_over_applet_when_both_available(self, tmp_path):
        # Real-world ordering decision (2026-07-06): after a machine
        # restart, terminal-notifier registered with macOS and worked,
        # while the applet did not visibly post -- so terminal-notifier
        # wins when both are present.
        applet = tmp_path / "AutonomousTraderNotifier.app"
        applet.mkdir()
        payload = tmp_path / "notification.txt"
        with (
            patch("src.notify.platform.system", return_value="Darwin"),
            patch("src.notify.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier"),
            patch("src.notify.subprocess.run") as mock_run,
            patch("src.notify.APPLET_PATH", applet),
            patch("src.notify.APPLET_PAYLOAD_PATH", payload),
        ):
            send_macos_notification("Title", "Message")
        assert mock_run.call_args[0][0][0] == "/opt/homebrew/bin/terminal-notifier"

    def test_quotes_pass_through_unchanged_no_script_parsing_involved(self, tmp_path):
        _, payload, _ = self._send(tmp_path, "Title", 'reasoning says "bullish"')
        assert 'reasoning says "bullish"' in payload.read_text(encoding="utf-8")


class TestCollapseWhitespace:
    def test_collapses_newlines_and_runs_of_spaces(self):
        assert _collapse_whitespace("a\nb   c") == "a b c"

    def test_plain_text_unchanged(self):
        assert _collapse_whitespace("AAPL buy x10") == "AAPL buy x10"
class TestFindTerminalNotifierUnderLaunchd:
    """Regression tests for the launchd-PATH failure (2026-07-07): the
    first successful scheduled cycle sent zero visible notifications
    because launchd's minimal PATH lacks Homebrew's bin dir, so
    shutil.which() found nothing even with terminal-notifier installed
    and working. The finder must fall back to Homebrew's known install
    locations directly."""

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value=None)
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_falls_back_to_homebrew_path_when_not_on_path(
        self, mock_platform, mock_which, mock_run, tmp_path
    ):
        fake_binary = tmp_path / "terminal-notifier"
        fake_binary.touch()
        with patch("src.notify.TERMINAL_NOTIFIER_FALLBACK_PATHS", (str(fake_binary),)):
            send_macos_notification("Title", "Message")
        assert mock_run.call_args[0][0][0] == str(fake_binary)

    @patch("src.notify.subprocess.run")
    @patch("src.notify.shutil.which", return_value="/from/path/terminal-notifier")
    @patch("src.notify.platform.system", return_value="Darwin")
    def test_path_lookup_still_wins_when_available(
        self, mock_platform, mock_which, mock_run, tmp_path
    ):
        fake_binary = tmp_path / "terminal-notifier"
        fake_binary.touch()
        with patch("src.notify.TERMINAL_NOTIFIER_FALLBACK_PATHS", (str(fake_binary),)):
            send_macos_notification("Title", "Message")
        assert mock_run.call_args[0][0][0] == "/from/path/terminal-notifier"
