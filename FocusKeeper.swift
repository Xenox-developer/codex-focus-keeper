import AppKit
import ApplicationServices
import Foundation

private let codexBundle = "com.openai.codex"
private var uptime: TimeInterval { ProcessInfo.processInfo.systemUptime }

/// Read only event ages and the Command modifier. Deferred retries also use the
/// age of the last keyboard/scroll event to yield if the user has started interacting.
/// This does not install an event tap, read pressed key codes, window titles,
/// typed text, screenshots or clipboard contents.
private final class UserIntent {
    private var allowUntil: TimeInterval = -.infinity

    func allowSwitch(for seconds: TimeInterval) { allowUntil = max(allowUntil, uptime + seconds) }

    static func plausibleManualSwitch(mouseAge: Double, modifierAge: Double,
                                      switcher: Bool, explicitGrace: Bool) -> Bool {
        // Deliberately err in favor of the user if an input and an activation overlap.
        mouseAge <= 0.38 || modifierAge <= 0.40 || switcher || explicitGrace
    }

    func manualSwitchLikely() -> Bool {
        let mouseAge = [CGEventType.leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp]
            .map { CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: $0) }.min()!
        let modifierAge = CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .flagsChanged)
        return Self.plausibleManualSwitch(mouseAge: mouseAge, modifierAge: modifierAge,
                                          switcher: CGEventSource.flagsState(.hidSystemState).contains(.maskCommand),
                                          explicitGrace: uptime < allowUntil)
    }
}

private enum RecoveryStep { case first, retry, yield }

private struct RecentRedirect {
    let pid: pid_t
    let time: TimeInterval
    let wasRetry: Bool

    static func next(after last: RecentRedirect?, target: pid_t, now: TimeInterval) -> RecoveryStep {
        guard let last = last, last.pid == target, now - last.time < 1 else { return .first }
        return last.wasRetry ? .yield : .retry
    }
}

private func axValue(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

private func appAX(_ pid: pid_t) -> AXUIElement {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.08)
    return app
}

private func focusedWindow(_ app: AXUIElement) -> AXUIElement? {
    guard let object = axValue(app, kAXFocusedWindowAttribute),
          CFGetTypeID(object) == AXUIElementGetTypeID() else { return nil }
    return (object as! AXUIElement)
}

private func plainWindow(_ window: AXUIElement) -> Bool {
    guard axValue(window, kAXSubroleAttribute) as? String == kAXStandardWindowSubrole else { return false }
    if (axValue(window, kAXModalAttribute) as? NSNumber)?.boolValue == true { return false }
    if (axValue(window, kAXMinimizedAttribute) as? NSNumber)?.boolValue == true { return false }
    if let sheets = axValue(window, "AXSheets") as? [Any], !sheets.isEmpty { return false }
    return true
}

private func isExcluded(_ app: NSRunningApplication) -> Bool {
    guard app.activationPolicy == .regular, let id = app.bundleIdentifier else { return true }
    // Never redirect system settings, login, authentication or password prompts.
    let reserved = ["com.apple.systempreferences", "com.apple.loginwindow", "com.apple.dock",
                    "com.apple.SecurityAgent", "com.apple.keychainaccess", "com.apple.Passwords",
                    "com.apple.CoreServicesUIAgent", "com.apple.ScreenSaver.Engine"]
    return reserved.contains(id) || id.hasPrefix("com.apple.LocalAuthentication")
        || id.hasPrefix("com.apple.AuthenticationServices") || id.hasPrefix("com.apple.Spotlight")
}

private final class FocusKeeper {
    private let observeOnly: Bool
    private let trusted = AXIsProcessTrusted()
    private let ws = NSWorkspace.shared
    private let intent = UserIntent()
    private var anchor: NSRunningApplication?
    private var anchorElement: AXUIElement?
    private var anchorWindow: AXUIElement?
    private var windowObserver: AXObserver?
    private var lastRedirect: RecentRedirect?
    private var generation: UInt64 = 0
    private var workspaceObservers = [NSObjectProtocol]()
    private var parentObserver: DispatchSourceProcess?
    private var sessionActive = true

    init(observeOnly: Bool) { self.observeOnly = observeOnly }

    private func report(_ line: String) {
        if observeOnly { print(line); fflush(stdout) }
    }

    func start() {
        let parent = getppid()
        guard parent > 1 else { exit(0) }
        let source = DispatchSource.makeProcessSource(identifier: parent, eventMask: .exit, queue: .main)
        source.setEventHandler { exit(0) }
        source.resume()
        parentObserver = source
        guard getppid() == parent else { exit(0) }

        let nc = ws.notificationCenter
        workspaceObservers.append(nc.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: .main) { [weak self] notification in
                guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
                else { return }
                self?.activation(app)
            })
        for name in [NSWorkspace.sessionDidResignActiveNotification, NSWorkspace.willSleepNotification] {
            workspaceObservers.append(nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.sessionActive = false
                self?.remember(nil)
            })
        }
        for name in [NSWorkspace.sessionDidBecomeActiveNotification, NSWorkspace.didWakeNotification] {
            workspaceObservers.append(nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                guard let self = self else { return }
                self.sessionActive = true
                self.intent.allowSwitch(for: 1)
                self.remember(self.ws.frontmostApplication)
            })
        }
        workspaceObservers.append(nc.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil, queue: .main) { [weak self] _ in
                guard let self = self else { return }
                self.intent.allowSwitch(for: 0.8)
                self.remember(self.ws.frontmostApplication)
            })
        remember(ws.frontmostApplication)
        report("watching; accessibility=\(trusted)")
    }

    private func remember(_ app: NSRunningApplication?) {
        generation &+= 1
        lastRedirect = nil
        if let observer = windowObserver {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        }
        windowObserver = nil
        anchor = nil
        anchorElement = nil
        anchorWindow = nil
        guard sessionActive, let app = app, !isExcluded(app), app.bundleIdentifier != codexBundle,
              !app.isHidden, !app.isTerminated else { return }
        anchor = app
        guard trusted else { return }
        let element = appAX(app.processIdentifier)
        anchorElement = element
        captureWindow()
        var next: AXObserver?
        let result = AXObserverCreate(app.processIdentifier, { _, _, _, refcon in
            guard let refcon = refcon else { return }
            Unmanaged<FocusKeeper>.fromOpaque(refcon).takeUnretainedValue().captureWindow()
        }, &next)
        guard result == .success, let observer = next else { return }
        let context = Unmanaged.passUnretained(self).toOpaque()
        guard AXObserverAddNotification(observer, element, kAXFocusedWindowChangedNotification as CFString, context) == .success
        else { return }
        windowObserver = observer
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
    }

    private func captureWindow() {
        guard let anchor = anchor, ws.frontmostApplication?.processIdentifier == anchor.processIdentifier,
              let element = anchorElement else { return }
        // Keep exactly the last observed focused window, even if temporarily unavailable.
        // A stale previous document must never be raised in place of a closed window or sheet.
        anchorWindow = focusedWindow(element)
    }

    private func activation(_ incoming: NSRunningApplication) {
        guard sessionActive, ws.frontmostApplication?.processIdentifier == incoming.processIdentifier else { return }
        generation &+= 1  // Invalidate delayed work from the previous foreground transition.
        guard let previous = anchor, previous.processIdentifier != incoming.processIdentifier else {
            if anchor == nil { remember(incoming) }
            return
        }
        let pid = incoming.processIdentifier
        if isExcluded(incoming) || previous.isTerminated || previous.isHidden || intent.manualSwitchLikely()
            || (incoming.launchDate.map { Date().timeIntervalSince($0) < 2 } ?? false) {
            report("accepted \(pid)")
            remember(incoming)
            return
        }
        switch RecentRedirect.next(after: lastRedirect, target: pid, now: uptime) {
        case .first:
            redirect(from: incoming, to: previous, retrySince: nil)
        case .retry:
            // A quick second activation can be a single app bringing more than one window
            // forward. Keep the original window and coalesce the burst before one more attempt.
            let began = uptime
            let event = generation
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.20) { [weak self] in
                guard let self = self, self.generation == event,
                      self.ws.frontmostApplication?.processIdentifier == pid else { return }
                self.redirect(from: incoming, to: previous, retrySince: began)
            }
        case .yield:
            // Avoid a focus fight. Do not mistake an insistent app for the user's selection.
            remember(nil)
        }
    }

    private func redirect(from incoming: NSRunningApplication, to previous: NSRunningApplication,
                          retrySince: TimeInterval?) {
        let start = uptime
        let pid = incoming.processIdentifier
        func userInteracted() -> Bool {
            if intent.manualSwitchLikely() { return true }
            guard let began = retrySince else { return false }
            let elapsed = uptime - began + 0.02
            if elapsed > 0.65 { return true }  // Delayed work after a stall is no longer safe.
            // Read timestamps only; do not read key codes or keystrokes. If the user started
            // typing or scrolling in the newly active app, leave it in place.
            return [CGEventType.keyDown, .keyUp, .flagsChanged, .scrollWheel, .leftMouseDown,
                    .leftMouseUp, .rightMouseDown, .rightMouseUp, .otherMouseDown, .otherMouseUp].contains {
                CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: $0) <= elapsed
            }
        }
        if userInteracted() { remember(incoming); return }
        let oldWindow = anchorWindow
        if trusted {
            // When a client or protected app doesn't expose an ordinary window, let it through.
            guard let newWindow = focusedWindow(appAX(pid)), plainWindow(newWindow),
                  let oldWindow = oldWindow, plainWindow(oldWindow) else {
                report("skipped nonstandard or unavailable window \(pid)")
                remember(incoming)
                return
            }
        } else if incoming.bundleIdentifier != codexBundle {
            // Without AX we cannot reliably identify dialogs in arbitrary applications.
            remember(incoming)
            return
        }
        // Reading AX can briefly block on a busy target. Revalidate before affecting the UI.
        guard uptime - start < 0.24, ws.frontmostApplication?.processIdentifier == pid,
              !previous.isTerminated, !previous.isHidden, !userInteracted() else {
            remember(ws.frontmostApplication)
            return
        }
        report("would restore \(previous.processIdentifier) after \(pid)")
        if observeOnly { remember(incoming); return }
        lastRedirect = RecentRedirect(pid: pid, time: uptime, wasRetry: retrySince != nil)
        let event = generation
        func stillExpected(_ foreground: pid_t) -> Bool {
            uptime - start < 0.27 && ws.frontmostApplication?.processIdentifier == foreground
                && !previous.isTerminated && !previous.isHidden && !userInteracted()
        }
        if trusted, let element = anchorElement, let oldWindow = oldWindow {
            // AX targets the existing window without simulating any key or pointer input.
            AXUIElementSetAttributeValue(oldWindow, kAXMainAttribute as CFString, kCFBooleanTrue)
            if stillExpected(pid) {
                AXUIElementSetAttributeValue(element, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
            }
            if ws.frontmostApplication?.processIdentifier == previous.processIdentifier,
               let current = focusedWindow(element), !CFEqual(current, oldWindow),
               stillExpected(previous.processIdentifier) {
                AXUIElementPerformAction(oldWindow, kAXRaiseAction as CFString)
            }
        }
        if stillExpected(pid) {
            _ = previous.activate(options: [])
        }
        // Do not let an old verification callback erase the anchor for a new activation.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.28) { [weak self] in
            guard let self = self, self.generation == event,
                  self.ws.frontmostApplication?.processIdentifier == pid else { return }
            // A rejected first activation request gets the same one bounded retry. That
            // retry rechecks the active window, modal state and user input before acting.
            if retrySince == nil {
                self.redirect(from: incoming, to: previous, retrySince: start)
            } else {
                self.remember(nil)
            }
        }
    }
}

private func selfTest() {
    typealias P = UserIntent
    precondition(!P.plausibleManualSwitch(mouseAge: 3, modifierAge: 4, switcher: false, explicitGrace: false))
    precondition(P.plausibleManualSwitch(mouseAge: 0.02, modifierAge: 4, switcher: false, explicitGrace: false))
    precondition(P.plausibleManualSwitch(mouseAge: 2, modifierAge: 0.05, switcher: false, explicitGrace: false))
    precondition(P.plausibleManualSwitch(mouseAge: 2, modifierAge: 4, switcher: true, explicitGrace: false))
    precondition(P.plausibleManualSwitch(mouseAge: 2, modifierAge: 4, switcher: false, explicitGrace: true))
    let one = RecentRedirect(pid: 42, time: 100, wasRetry: false)
    let two = RecentRedirect(pid: 42, time: 100.2, wasRetry: true)
    precondition(RecentRedirect.next(after: nil, target: 42, now: 100) == .first)
    precondition(RecentRedirect.next(after: one, target: 42, now: 100.15) == .retry)
    precondition(RecentRedirect.next(after: two, target: 42, now: 100.35) == .yield)
    precondition(RecentRedirect.next(after: two, target: 43, now: 100.35) == .first)
    precondition(RecentRedirect.next(after: two, target: 42, now: 101.21) == .first)
    print("Intent and bounded retry checks passed.")
}

switch Array(CommandLine.arguments.dropFirst()) {
case ["--self-test"]:
    selfTest()
case ["--status"]:
    print("Accessibility permission: \(AXIsProcessTrusted() ? "available" : "unavailable; application-only fallback for Codex activation").")
case [], ["--observe-only"]:
    let keeper = FocusKeeper(observeOnly: CommandLine.arguments.contains("--observe-only"))
    keeper.start()
    if !CommandLine.arguments.contains("--observe-only") {
        FileHandle.standardOutput.write(Data([49]))  // Supervisor start handshake, no newline.
    }
    withExtendedLifetime(keeper) { RunLoop.main.run() }
default:
    fputs("Usage: FocusKeeper [--self-test | --status | --observe-only]\n", stderr)
    exit(2)
}
