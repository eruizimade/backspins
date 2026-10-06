// Backspins — the toolkit as a Mac app, not a browser tab.
//
// A window with a WebKit view showing the same interface the server has always
// served. The app owns nothing of the library: it finds the toolkit server (or
// starts it), points the window at it, and stops it again on Quit if it was the
// one that started it. Everything else — the screens, the queue, the menu bar
// popover — is exactly what it was in Chrome.
//
// Why WebKit and not a browser:
//   · It is its own app: its own window, Dock icon and ⌘-Tab entry, no tabs,
//     no address bar, nothing to close by accident.
//   · It plays AIFF itself (Core Audio), which Chrome never did.
//   · Autoplay and background timers are ours to decide. A browser tab in the
//     background is throttled and refuses to start playback — which is what
//     made "done, next" from the menu bar go silent.
//
// Built by build.sh (swiftc, no Xcode project). The toolkit's folder is written
// into Info.plist as ToolkitDir at build time.

import Cocoa
@preconcurrency import WebKit

let FIRST_PORT = 8765
let PORT_SPAN = 20
let BOOTH = NSColor(srgbRed: 0x12 / 255.0, green: 0x14 / 255.0, blue: 0x18 / 255.0, alpha: 1)
let DAYLIGHT = NSColor(srgbRed: 0xf5 / 255.0, green: 0xf5 / 255.0, blue: 0xf7 / 255.0, alpha: 1)
// `--port N`: talk to that server only, and never start one. For trying a
// build against a test server without touching the one in use.
let FIXED_PORT: Int? = {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: "--port"), i + 1 < a.count { return Int(a[i + 1]) }
    return nil
}()
// Test hooks, for checking a build without screen-recording permission:
// `--probe FILE.js` runs FILE.js in the page once it has loaded (as the body of
// an async function) and writes what it returns to FILE.js.out; a result of
// the form {"snapshot": "/path.png"} also saves a picture of the window there.
func argValue(_ name: String) -> String? {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: name), i + 1 < a.count { return a[i + 1] }
    return nil
}

// A private WebKit switch, set if this WebKit has it and skipped if not.
func setPrivateBool(_ obj: NSObject, _ name: String, _ value: Bool) {
    let sel = NSSelectorFromString(name)
    guard obj.responds(to: sel), let m = class_getInstanceMethod(type(of: obj), sel) else { return }
    typealias Setter = @convention(c) (AnyObject, Selector, Bool) -> Void
    unsafeBitCast(method_getImplementation(m), to: Setter.self)(obj, sel, value)
}

// The strips you can drag the window by. The page fills the whole window,
// title bar included, and a web view never lets a click move its window — so
// these sit on top of the two places that are never controls: the top of the
// sidebar, under the traffic lights, and a thin band along the top edge.
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            let action = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize"
            if action == "Minimize" { window?.performMiniaturize(nil) }
            else if action != "None" { window?.performZoom(nil) }
        } else {
            window?.performDrag(with: event)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?          // only if WE started it
    var port: Int?
    var toolDir = ""
    let log = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Backspins.log")

    // ── start ──────────────────────────────────────────────────────────────
    func applicationDidFinishLaunching(_ note: Notification) {
        toolDir = (Bundle.main.object(forInfoDictionaryKey: "ToolkitDir") as? String) ?? ""
        if toolDir.isEmpty || !FileManager.default.fileExists(atPath: toolDir + "/convertidor.py") {
            // Built next to the toolkit: fall back on the folder the app sits in.
            toolDir = Bundle.main.bundleURL.deletingLastPathComponent().path
        }
        buildMenu()
        buildWindow()
        // ⚠ The toolkit lives in Documents, which macOS guards: the first time
        // (and after every rebuild — an ad-hoc signature is a new app to it)
        // it asks, and the start waits on that answer.
        showMessage("Starting…",
                    detail: "If macOS asks, allow access to the Documents folder: the toolkit lives there.")
        DispatchQueue.global(qos: .userInitiated).async { self.connect() }
    }

    func buildWindow() {
        let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable,
                                         .fullSizeContentView]
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: style, backing: .buffered, defer: false)
        window.title = "Backspins"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 1000, height: 640)
        window.delegate = self
        window.backgroundColor = NSColor(name: nil) { ap in
            ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? BOOTH : DAYLIGHT
        }
        window.center()
        window.setFrameAutosaveName("RekordboxToolkitMain")

        let cfg = WKWebViewConfiguration()
        // Playback starts when the queue says so — "done, next" from the menu
        // bar has no click in this window to lean on.
        cfg.mediaTypesRequiringUserActionForPlayback = []
        // The page knows it is inside the app by this, and makes room for the
        // traffic lights.
        cfg.applicationNameForUserAgent = "Version/18.0 Safari/605.1.15 RekordboxToolkit/1.0"
        let prefs = cfg.preferences
        // ⚠ A hidden page's timers are slowed to a crawl, and WebKit may put its
        // whole process to sleep. The window behind rekordbox is exactly when
        // the menu bar is driving it, so neither may happen here.
        setPrivateBool(prefs, "_setHiddenPageDOMTimerThrottlingEnabled:", false)
        setPrivateBool(prefs, "_setHiddenPageDOMTimerThrottlingAutoIncreases:", false)
        setPrivateBool(prefs, "_setPageVisibilityBasedProcessSuppressionEnabled:", false)
        setPrivateBool(prefs, "_setDeveloperExtrasEnabled:", true)

        web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")   // no white flash on load
        if #available(macOS 13.3, *) { web.isInspectable = true }

        let root = NSView(frame: window.contentLayoutRect)
        root.autoresizingMask = [.width, .height]
        web.frame = root.bounds
        web.autoresizingMask = [.width, .height]
        root.addSubview(web)

        let h = root.bounds.height
        let band = DragStrip(frame: NSRect(x: 0, y: h - 10, width: root.bounds.width, height: 10))
        band.autoresizingMask = [.width, .minYMargin]
        root.addSubview(band)
        let corner = DragStrip(frame: NSRect(x: 0, y: h - 46, width: 212, height: 46))
        corner.autoresizingMask = [.maxXMargin, .minYMargin]
        root.addSubview(corner)

        window.contentView = root
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showMessage(_ title: String, detail: String) {
        let html = """
        <!doctype html><meta charset=utf-8><style>
        :root{color-scheme:light dark}
        body{margin:0;height:100vh;display:grid;place-items:center;
             font:15px -apple-system,sans-serif;color:#6e6e73;background:#f5f5f7}
        @media (prefers-color-scheme:dark){body{background:#121418;color:#a4abb8}}
        b{display:block;font-size:17px;font-weight:600;color:inherit;margin-bottom:6px;text-align:center}
        p{margin:0;max-width:460px;text-align:center;line-height:1.5}
        </style><div><b>\(title)</b><p>\(detail)</p></div>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // ── the server ─────────────────────────────────────────────────────────
    // Use one that is already running (the old launcher, or another window);
    // otherwise start it, and remember that it is ours to stop.
    func connect() {
        // Test hook: start a server of our own on this port and use only it —
        // the path a launch from the Dock takes, tried next to a running one.
        if let sp = argValue("--spawn-port").flatMap({ Int($0) }) {
            guard startServer(extra: ["--port", String(sp)]) else { return }
            let until = Date().addingTimeInterval(45)
            while Date() < until {
                if probe(sp) { open(sp); return }
                Thread.sleep(forTimeInterval: 0.15)
            }
            return
        }
        if let fp = FIXED_PORT {
            let until = Date().addingTimeInterval(20)
            while Date() < until {
                if probe(fp) { open(fp); return }
                Thread.sleep(forTimeInterval: 0.3)
            }
            DispatchQueue.main.async {
                self.showMessage("Nothing answering on port \(fp)", detail: "")
            }
            return
        }
        if let p = findServer() { open(p); return }
        guard startServer() else {
            DispatchQueue.main.async {
                self.showMessage("Could not start the toolkit",
                                 detail: "No working Python found, or the toolkit folder moved: \(self.toolDir)")
            }
            return
        }
        let until = Date().addingTimeInterval(45)
        while Date() < until {
            if let s = server, !s.isRunning { break }
            if let p = findServer() { open(p); return }
            Thread.sleep(forTimeInterval: 0.15)
        }
        DispatchQueue.main.async {
            self.showMessage("The toolkit did not start",
                             detail: "Its log is at ~/Library/Logs/Backspins.log")
        }
    }

    func open(_ p: Int) {
        port = p
        DispatchQueue.main.async {
            self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(p)/")!))
        }
    }

    func findServer() -> Int? {
        for p in FIRST_PORT..<(FIRST_PORT + PORT_SPAN) where probe(p) { return p }
        return nil
    }

    // ⚠ Not "the port answers" but "the TOOLKIT answers": /api/lib-gen is ours.
    func probe(_ p: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(p)/api/lib-gen") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.4
        let done = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let d = data, (resp as? HTTPURLResponse)?.statusCode == 200,
               let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               j["gen"] != nil { ok = true }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 0.6)
        return ok
    }

    // The same interpreter choice as "Convertidor de musica.command": the first
    // python that really runs (some Macs carry one on the PATH that does not).
    func python() -> String? {
        let candidates = [toolDir + "/.venv/bin/python3", "/opt/homebrew/bin/python3",
                          "/usr/local/bin/python3", "/usr/bin/python3"]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            let t = Process()
            t.executableURL = URL(fileURLWithPath: c)
            t.arguments = ["-c", "print(42)"]
            let out = Pipe()
            t.standardOutput = out
            t.standardError = FileHandle.nullDevice
            do { try t.run() } catch { continue }
            t.waitUntilExit()
            let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            if s.trimmingCharacters(in: .whitespacesAndNewlines) == "42" { return c }
        }
        return nil
    }

    func startServer(extra: [String] = []) -> Bool {
        guard FileManager.default.fileExists(atPath: toolDir + "/convertidor.py"),
              let py = python() else { return false }
        let t = Process()
        t.executableURL = URL(fileURLWithPath: py)
        t.arguments = ["convertidor.py", "--no-browser"] + extra
        t.currentDirectoryURL = URL(fileURLWithPath: toolDir)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        t.environment = env
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: log) {
            t.standardOutput = fh
            t.standardError = fh
        }
        do { try t.run() } catch { return false }
        server = t
        return true
    }

    // ⚠ Quit takes the server with it — and the server takes its menu bar icon
    // with it — but only if this app started it. One left behind by the old
    // launcher belongs to whoever started it.
    func applicationWillTerminate(_ note: Notification) {
        guard let s = server, s.isRunning else { return }
        s.terminate()
        let until = Date().addingTimeInterval(3)
        while s.isRunning && Date() < until { Thread.sleep(forTimeInterval: 0.05) }
    }

    // ── the window ─────────────────────────────────────────────────────────
    // Closing the window only hides it: the player lives in it, and the music
    // (and the menu bar remote) should carry on. The Dock icon brings it back.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    var probed = false
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !probed, port != nil, let file = argValue("--probe"),
              let js = try? String(contentsOfFile: file, encoding: .utf8) else { return }
        probed = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            webView.callAsyncJavaScript(js, arguments: [:], in: nil, in: .page) { result in
                var text = ""
                switch result {
                case .success(let v): text = "\(v)"
                case .failure(let e): text = "ERROR: \(e)"
                }
                if let d = text.data(using: .utf8),
                   let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   let snap = j["snapshot"] as? String {
                    webView.takeSnapshot(with: nil) { img, _ in
                        if let img = img, let tiff = img.tiffRepresentation,
                           let rep = NSBitmapImageRep(data: tiff),
                           let png = rep.representation(using: .png, properties: [:]) {
                            try? png.write(to: URL(fileURLWithPath: snap))
                        }
                        try? text.write(toFile: file + ".out", atomically: true, encoding: .utf8)
                    }
                } else {
                    try? text.write(toFile: file + ".out", atomically: true, encoding: .utf8)
                }
            }
        }
    }

    // A crashed web process is a reload, not a white window.
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if let p = port { open(p) }
    }

    // ── links: ours stay here, everything else goes to your browser ────────
    // The pool is the one that matters: its session is in YOUR browser, and
    // downloading there is the whole point.
    func isOurs(_ url: URL?) -> Bool {
        guard let u = url else { return true }
        if u.scheme == "about" || u.scheme == "data" || u.scheme == "blob" { return true }
        return u.host == "127.0.0.1" || u.host == "localhost"
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if !isOurs(action.request.url), let u = action.request.url {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url {
            if isOurs(u) { webView.load(action.request) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }

    // ── the page's alert / confirm / prompt, as sheets ─────────────────────
    // ⚠ Without these a web view answers every confirm() with "no", and the
    // toolkit asks before anything that writes in bulk.
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        let parts = message.components(separatedBy: "\n\n")
        a.messageText = parts.first ?? message
        if parts.count > 1 { a.informativeText = parts.dropFirst().joined(separator: "\n\n") }
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in
            completionHandler(r == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = parameters.allowsMultipleSelection
        p.canChooseDirectories = parameters.allowsDirectories
        p.beginSheetModal(for: window) { r in completionHandler(r == .OK ? p.urls : nil) }
    }

    // ── menus ──────────────────────────────────────────────────────────────
    // ⚠ The Edit menu is not decoration: without it ⌘C / ⌘V / ⌘A do nothing in
    // a web view, and you could not paste a search.
    @objc func reload(_ sender: Any?) {
        if let p = port { open(p) } else { DispatchQueue.global().async { self.connect() } }
    }
    @objc func openInBrowser(_ sender: Any?) {
        if let p = port, let u = URL(string: "http://127.0.0.1:\(p)/") { NSWorkspace.shared.open(u) }
    }
    @objc func showLog(_ sender: Any?) { NSWorkspace.shared.open(log) }

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String) -> NSMenu {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            item.submenu = m
            main.addItem(item)
            return m
        }
        let app = sub("Backspins")
        app.addItem(withTitle: "About Backspins",
                    action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Backspins", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let others = app.addItem(withTitle: "Hide Others",
                                 action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        others.keyEquivalentModifierMask = [.command, .option]
        app.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit Backspins", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let edit = sub("Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let view = sub("View")
        let r = view.addItem(withTitle: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
        r.target = self
        let b = view.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser(_:)), keyEquivalent: "")
        b.target = self
        let l = view.addItem(withTitle: "Show Server Log", action: #selector(showLog(_:)), keyEquivalent: "")
        l.target = self
        view.addItem(.separator())
        let fs = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.command, .control]

        let win = sub("Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
