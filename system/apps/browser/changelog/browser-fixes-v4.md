Failing to pull a browser's live pane is no longer reported as an error.

When the fleet starts a browser it optimistically tries to split its live view
into the current agent's chat. That split only lands when a client is actually
watching this agent's chat, so for a background or sub-agent it routinely does
not -- and the previous message ("I couldn't open its live pane here...") framed
that expected outcome as a failure, implying something had broken when the
browser was up and fully drivable from the CLI.

The fallback is now informational and goes to stdout rather than stderr:
"browser <name> is ready. To watch it live, open it from the '+' menu (New
browser -> <name>) in the side panel." The pane is a convenience, not a
precondition for using the browser.

----

The live view is now KasmVNC, and the browser pane shows the real Chromium.

Each browser gets its own `Xvnc` -- KasmVNC's X server, which is also the web
client that streams it -- and Chromium is launched headful into it. The pane
embeds that client, so what you see is the whole browser window: its real tab
strip, its real back/forward buttons, its real address bar.

**Input works properly now.** Mouse and keyboard travel down the VNC connection
and are injected as X events at the display level, so native right-click context
menus, native `<select>` dropdowns and date pickers, text selection and real
click-drag all behave like a normal browser. None of those could work before:
CDP's page-scoped input events cannot reach browser-native UI.

**What's gone from the pane:** the hand-built tab strip, back/forward/reload
buttons and address bar. They existed only because the old CDP screencast
captured the page viewport and not Chromium's own chrome, so they had to be
rebuilt in HTML. They would now be a second, worse copy stacked above the real
thing. Chromium's own chrome replaces all of them.

**What's unchanged:** the "An agent has control" overlay, Take control, the
"You have control" bar and Return control, the starting spinner, and the crashed
state. Agents still drive over CDP exactly as before, and the fleet's naming,
cap, and `+` menu behave identically.

Two consequences worth knowing:

- There is no window manager on these displays, so `document.hasFocus()` is
  false in the page. Clicking and typing work; the JS Clipboard API and some
  autofocus behaviours do not.
- Chromium's address bar can reach `chrome://` pages, downloads and devtools.
  The old view had no address bar, so that surface was previously unreachable.
