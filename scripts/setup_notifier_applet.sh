#!/bin/bash
# One-time build of the notification applet src/notify.py prefers.
#
# Why this exists: on current macOS, both notification backends that
# don't involve a real app identity are broken -- bare
# `osascript -e 'display notification ...'` exits 0 and posts nothing, and
# terminal-notifier (unmaintained since 2019) does the same, never even
# registering itself in System Settings > Notifications (both confirmed on
# this machine, 2026-07-06). A locally-compiled AppleScript applet is a
# real .app with its own bundle identity, so macOS gives it a genuine
# permission prompt and a Notifications settings entry.
#
# The applet takes no arguments (applets can't); it reads its payload from
# ~/Library/Application Support/autonomous-trader/notification.txt
# (line 1 title, line 2 message, line 3 optional subtitle), which
# src/notify.py writes immediately before launching it with `open -g -W`.
#
# Run:  bash scripts/setup_notifier_applet.sh
# Then send a test notification and click Allow on the permission prompt:
#   python -c "from src.notify import send_macos_notification as n; n('Autonomous Trader', 'test notification')"
set -euo pipefail

APP="$HOME/Applications/AutonomousTraderNotifier.app"

mkdir -p "$HOME/Applications"
rm -rf "$APP"

osacompile -o "$APP" <<'APPLESCRIPT'
on run
	set payloadPath to (POSIX path of (path to home folder)) & "Library/Application Support/autonomous-trader/notification.txt"
	try
		set payload to read POSIX file payloadPath as «class utf8»
	on error
		display notification "notifier applet ran, but no payload file was found" with title "Autonomous Trader"
		return
	end try

	set AppleScript's text item delimiters to linefeed
	set payloadLines to text items of payload

	set theTitle to "Autonomous Trader"
	set theMessage to ""
	set theSubtitle to ""
	if (count of payloadLines) >= 1 then set theTitle to item 1 of payloadLines
	if (count of payloadLines) >= 2 then set theMessage to item 2 of payloadLines
	if (count of payloadLines) >= 3 then set theSubtitle to item 3 of payloadLines

	if theSubtitle is not "" then
		display notification theMessage with title theTitle subtitle theSubtitle
	else
		display notification theMessage with title theTitle
	end if
end run
APPLESCRIPT

echo "Built $APP"
echo
echo "Now send a test notification (from the project root):"
echo "  python -c \"from src.notify import send_macos_notification as n; n('Autonomous Trader', 'test notification')\""
echo
echo "The FIRST run should show a macOS permission prompt for"
echo "'AutonomousTraderNotifier' -- click Allow. After that it appears in"
echo "System Settings > Notifications under that name (set style to Alerts"
echo "or Banners). src/notify.py picks the applet up automatically on the"
echo "next cycle -- no code change or reload needed."
