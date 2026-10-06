#!/usr/bin/env python3
"""A menu bar icon that tags whatever the Tagger has open.

Why this exists: tagging happens while you LISTEN, and listening happens with
some other window in front — rekordbox, a browser, anything. Switching to the
toolkit to tick one tag breaks the listening. This puts the tag list one click
away, always, without taking the front window away from you.

It is deliberately thin. It owns no data and knows nothing about your library:
it is a small native window showing `/menubar` from the server that is already
running, and every click goes to the same queue the Tagger writes to. Anything
you tag here is waiting in the same place, and syncs with the same button.

Run it next to the server (the launcher does this for you):

    ./.venv/bin/python3 menubar.py

Quitting it changes nothing — the server, the queue and the Tagger carry on.
"""

import json
import math
import os
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request

try:
    import objc
    from AppKit import (NSApplication, NSStatusBar, NSPopover, NSViewController,
                        NSVariableStatusItemLength, NSPopoverBehaviorTransient,
                        NSMenu, NSMenuItem, NSApplicationActivationPolicyAccessory,
                        NSRectEdgeMinY, NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp,
                        NSEventTypeRightMouseUp, NSEventTypeRightMouseDown,
                        NSEventModifierFlagControl, NSImage, NSBezierPath, NSColor,
                        NSGraphicsContext, NSCompositingOperationClear,
                        NSCompositingOperationSourceOver, NSScreen)
    from WebKit import WKWebView, WKWebViewConfiguration
    from Foundation import (NSObject, NSMakeRect, NSURL, NSURLRequest, NSSize,
                            NSPoint)
except ImportError:
    sys.exit('The menu bar needs PyObjC:\n'
             '    ./.venv/bin/python3 -m pip install '
             'pyobjc-framework-Cocoa pyobjc-framework-WebKit')

# ⚠ The port is DISCOVERED, never assumed. convertidor.py walks forward from
# 8765 through 20 ports when its own is busy, so a second copy of the toolkit
# (or a leftover server) pushes the real one to 8768 and the menu bar ends up
# talking to an instance you do not have on screen: it shows "Nothing open"
# while you are staring at an open track. That is exactly what happened.
FIRST_PORT = int(os.environ.get('TOOLKIT_PORT', '8765'))
PORT_SPAN = 20
# A lock port well outside the server's range, so it cannot collide with one.
LOCK_PORT = int(os.environ.get('TOOLKIT_MENUBAR_LOCK', '47653'))   # override only to test a second copy
# ⚠ Tall enough for the whole track — rating, energy, every tag bank and the
# finishing button — without scrolling. At 480 the MOOD bank, where most of
# the ten-odd taps per track go, started below the fold on every track.
POPOVER_SIZE = (380, 700)

# The icon is Backspins's mark: a record with an arrow taking it backwards. It
# is DRAWN rather than shipped as a .png: bezier paths are sharp at any scale,
# there is no asset to keep in step with the code, and as a template image
# macOS tints it itself — dark menu bar, light menu bar, and inverted while
# the popover is open.
ICON_BOX = 18.0     # points square; the menu bar gives about 22
ICON_INSET = 1.0    # breathing room, so it sits like the system's own icons
ICON_DISC = 0.58    # the record, as a fraction of the radius
ICON_LABEL = 0.22   # the hole cut in its middle
ICON_STROKE = 0.16  # the arrow's thickness


def menubar_icon():
    """The record and its backspin arrow, as a menu bar template image."""

    def draw(rect):
        c = ICON_BOX / 2.0
        r = c - ICON_INSET
        # A template image keeps only the ALPHA, so the colour is arbitrary.
        NSColor.blackColor().setFill()
        NSColor.blackColor().setStroke()
        disc = ICON_DISC * r
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(c - disc, c - disc, disc * 2, disc * 2)).fill()
        ctx = NSGraphicsContext.currentContext()
        ctx.setCompositingOperation_(NSCompositingOperationClear)
        lab = ICON_LABEL * r
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(c - lab, c - lab, lab * 2, lab * 2)).fill()
        ctx.setCompositingOperation_(NSCompositingOperationSourceOver)
        # Most of a turn, counter-clockwise — backwards on a deck.
        w = ICON_STROKE * r
        ar = r - w / 2.0
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            NSPoint(c, c), ar, -60, 190, False)
        arc.setLineWidth_(w)
        arc.setLineCapStyle_(1)
        arc.stroke()
        a = math.radians(190)
        px, py = c + ar * math.cos(a), c + ar * math.sin(a)
        t = a + math.pi / 2
        head = w * 1.9
        tri = NSBezierPath.bezierPath()
        tri.moveToPoint_(NSPoint(px + head * math.cos(t), py + head * math.sin(t)))
        tri.lineToPoint_(NSPoint(px + head * 0.7 * math.cos(a), py + head * 0.7 * math.sin(a)))
        tri.lineToPoint_(NSPoint(px - head * 0.7 * math.cos(a), py - head * 0.7 * math.sin(a)))
        tri.closePath()
        tri.fill()
        return True

    img = NSImage.imageWithSize_flipped_drawingHandler_(
        NSSize(ICON_BOX, ICON_BOX), False, draw)
    img.setTemplate_(True)
    return img


def find_server():
    """The port the toolkit is actually on, or None.

    If more than one instance answers, the one that knows which track you have
    open is the one you are using — prefer it over whichever booted first.
    """
    if os.environ.get('TOOLKIT_PORT'):
        return FIRST_PORT if probe(FIRST_PORT) is not None else None
    answered = []
    for p in range(FIRST_PORT, FIRST_PORT + PORT_SPAN):
        got = probe(p)
        if got is not None:
            answered.append((p, got))
    if not answered:
        return None
    # ⚠ Disambiguate by RECENCY, not by "has a track". With two servers up — a
    # stray one from an earlier launch is easy to end up with — both can have a
    # track, and the older one wins a first-match test while you are clicking
    # in the other. The freshest report is the window you are actually using.
    live = [(g.get('at') or '', p) for p, g in answered if g.get('track')]
    if live:
        return max(live)[1]
    return answered[0][0]


def probe(port):
    """Ask a port whether it is the toolkit. Returns its 'now' payload or None."""
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/api/tagger/now' % port, timeout=0.6) as r:
            return json.loads(r.read() or b'{}')
    except Exception:
        return None


def alive(port):
    """Is the toolkit still on this port? The cheap question: one tiny route."""
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/api/lib-gen' % port, timeout=0.25) as r:
            return r.status == 200
    except Exception:
        return False


# The Mac app (app/main.swift). Overridable only to test against a copy.
# The packaged app (backspins.spec) has the same id.
APP_IDS = ('app.backspins.mac', 'app.backspin.mac')   # 0.1.x had the second
if os.environ.get('TOOLKIT_APP_ID'):
    APP_IDS = (os.environ['TOOLKIT_APP_ID'],)


def _server_pids(port):
    """The toolkit servers to stop: the one that started this icon, and the one
    on the port it talks to — usually the same process. Only ever a
    convertidor.py: never something else that happens to own the port."""
    pids = {os.getppid()}
    if port:
        try:
            out = subprocess.run(['/usr/sbin/lsof', '-t', '-iTCP:%d' % port,
                                  '-sTCP:LISTEN'], capture_output=True,
                                 text=True, timeout=3).stdout
            pids.update(int(x) for x in out.split() if x.strip().isdigit())
        except Exception:
            pass
    keep = set()
    for pid in pids:
        try:
            cmd = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout
        except Exception:
            cmd = ''
        if 'convertidor.py' in cmd:
            keep.add(pid)
    return keep


def quit_everything(port):
    """Close the whole toolkit: the app, the server, and this icon.

    ⚠ All three, whichever way it was started. Quitting the app stops the
    server it started, and the server takes this icon with it — but a server
    started by the old launcher belongs to no app, and would carry on alone.
    """
    try:
        from AppKit import NSRunningApplication
        for app_id in APP_IDS:
            for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(app_id):
                app.terminate()
    except Exception:
        pass
    for pid in _server_pids(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    NSApplication.sharedApplication().terminate_(None)


def claim_single_instance():
    """Return the socket that makes this the ONE menu bar, or None.

    Two icons doing the same thing is confusing, and easy to end up with: the
    launcher starts one and you start another by hand. Holding a loopback port
    is the cheapest lock there is — it disappears on its own when the process
    does, so a crash can never leave a stale lock behind (a PID file can).
    """
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(('127.0.0.1', LOCK_PORT))
        lock.listen(1)
        return lock
    except OSError:
        lock.close()
        return None





class SizeHandler(NSObject, protocols=[objc.protocolNamed('WKScriptMessageHandler')]):
    """The page talking to the icon: how tall it wants to be (reportSize in
    menubar.html), and its Quit button."""

    def initWithController_(self, ctl):
        self = objc.super(SizeHandler, self).init()
        if self is None:
            return None
        self.ctl = ctl
        return self

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        if message.name() == 'quit':
            self.ctl.quitAll_(None)
            return
        try:
            self.ctl.fitHeight_(float(message.body()))
        except Exception:
            pass


class Controller(NSObject):

    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None

        cfg = WKWebViewConfiguration.alloc().init()
        self.sizer = SizeHandler.alloc().initWithController_(self)
        cfg.userContentController().addScriptMessageHandler_name_(self.sizer, 'size')
        cfg.userContentController().addScriptMessageHandler_name_(self.sizer, 'quit')
        self.web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, *POPOVER_SIZE), cfg)
        # The popover is chrome, not a web page: no rubber-band scroll past the
        # ends, and no right-click "Reload" menu inside a menu bar panel.
        try:
            self.web.setValue_forKey_(False, 'drawsBackground')
        except Exception:
            pass

        vc = NSViewController.alloc().init()
        vc.setView_(self.web)

        self.popover = NSPopover.alloc().init()
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_(NSSize(*POPOVER_SIZE))
        # Transient: clicking anywhere else dismisses it, like every other menu
        # bar app. You tag, you click away, you carry on listening.
        self.popover.setBehavior_(NSPopoverBehaviorTransient)

        bar = NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.item.button().setImage_(menubar_icon())
        self.item.button().setToolTip_('Tag what the Tagger has open')
        self.item.button().setTarget_(self)
        self.item.button().setAction_('clicked:')
        self.item.button().sendActionOn_(
            NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)

        self.menu = NSMenu.alloc().init()
        opener = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Open Backspins', 'openApp:', '')
        opener.setTarget_(self)
        self.menu.addItem_(opener)
        self.menu.addItem_(NSMenuItem.separatorItem())
        # The whole thing, not just this icon: an icon quitting on its own left
        # the app and the server running with nothing to drive them from.
        quitter = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Quit Backspins', 'quitAll:', 'q')
        quitter.setTarget_(self)
        self.menu.addItem_(quitter)
        self.loaded = None          # the port the page in the popover came from
        return self

    def fitHeight_(self, h):
        """As tall as the page needs, and never taller than the screen."""
        screen = None
        try:
            screen = self.item.button().window().screen()
        except Exception:
            pass
        screen = screen or NSScreen.mainScreen()
        top = screen.visibleFrame().size.height - 14 if screen else 900
        h = max(360.0, min(float(h), top))
        if abs(h - self.popover.contentSize().height) >= 2:
            self.popover.setContentSize_(NSSize(POPOVER_SIZE[0], h))

    def loadPort_(self, port):
        self.web.loadRequest_(NSURLRequest.requestWithURL_(
            NSURL.URLWithString_('http://127.0.0.1:%d/menubar' % port)))
        self.loaded = port

    # ── actions ──────────────────────────────────────────────────────────
    def clicked_(self, sender):
        ev = NSApplication.sharedApplication().currentEvent()
        # Right-click (or ctrl-click) is the housekeeping menu; left-click is
        # the thing you actually came for.
        right = ev is not None and (
            ev.type() in (NSEventTypeRightMouseUp, NSEventTypeRightMouseDown)
            or bool(ev.modifierFlags() & NSEventModifierFlagControl))
        if right:
            self.item.popUpStatusItemMenu_(self.menu)
            return
        if self.popover.isShown():
            self.popover.performClose_(sender)
            return
        # ⚠ Fast path first. This used to scan all twenty ports and reload the
        # whole page on EVERY open, on the main thread — the popover sat there
        # blank while it happened, and that is why it was slow to open. Now:
        # one tiny request to the port we already know, and the page, which
        # stays loaded, only fetches the current track. The full scan is only
        # for when that port has stopped answering (the toolkit restarted
        # somewhere else, say), so the icon still follows it.
        port = self.loaded if (self.loaded and alive(self.loaded)) else find_server()
        if port is None:
            self.item.button().setToolTip_('The toolkit is not running')
            return
        if port != self.loaded:
            self.loadPort_(port)
        else:
            self.web.evaluateJavaScript_completionHandler_(
                'window.__shown && window.__shown()', None)
        btn = self.item.button()
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            btn.bounds(), btn, NSRectEdgeMinY)
        self.popover.contentViewController().view().window().makeFirstResponder_(self.web)

    def openApp_(self, sender):
        port = find_server()
        if port is None:
            return
        url = 'http://127.0.0.1:%d/' % port
        # ⚠ The SAME place the server opens (see open_in_browser there): the
        # Mac app if it is installed, else Chrome. The toolkit window is the
        # player, and opening a second one somewhere else gives you two players
        # quietly fighting over the same queue.
        names = ['Backspins', 'Rekordbox Toolkit']
        for cmd in [['open', '-a', n] for n in names] + [['open', '-a', 'Google Chrome', url]]:
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)
                return
            except Exception:
                pass
        from AppKit import NSWorkspace
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    def quitAll_(self, sender):
        try:
            self.popover.performClose_(None)
        except Exception:
            pass
        quit_everything(self.loaded)


def main():
    lock = claim_single_instance()
    if lock is None:
        sys.exit('The menu bar is already running (look for the cube up top).')
    port = find_server()
    if port is None:
        sys.exit('No toolkit server answering on ports %d-%d.\n'
                 'Start it first (watch.py or the launcher), then run this.'
                 % (FIRST_PORT, FIRST_PORT + PORT_SPAN - 1))
    app = NSApplication.sharedApplication()
    # Accessory: lives in the menu bar only — no Dock icon, no app switcher
    # entry. It is a control, not a window you manage.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    controller = Controller.alloc().init()
    # Load the popover's page now, so even the first open is instant.
    controller.loadPort_(port)
    app.setDelegate_(controller)
    app.run()


if __name__ == '__main__':
    main()
