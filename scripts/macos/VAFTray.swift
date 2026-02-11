import Cocoa

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var vafProcess: Process?
    var vafDir: String = ""
    var frontendPort: String = "3000"
    var statusMenuItem: NSMenuItem!
    var checkTimer: Timer?
    
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Get VAF directory (parent of MacOS directory in app bundle, or fallback)
        let bundle = Bundle.main
        if let resourcePath = bundle.resourcePath {
            // App bundle: VAF.app/Contents/Resources -> check for vaf_dir.txt
            let dirFile = (resourcePath as NSString).appendingPathComponent("vaf_dir.txt")
            if let dir = try? String(contentsOfFile: dirFile, encoding: .utf8) {
                vafDir = dir.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        if vafDir.isEmpty {
            // Fallback: assume ~/VAF
            vafDir = NSHomeDirectory() + "/VAF"
        }
        
        // Create Status Bar Item (Tray Icon)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        
        if let button = statusItem.button {
            // Try to load custom icon
            let iconPath = NSHomeDirectory() + "/.vaf/icons/tray_v2_idle.png"
            if FileManager.default.fileExists(atPath: iconPath),
               let image = NSImage(contentsOfFile: iconPath) {
                image.isTemplate = false  // Keep original colors (VAF logo)
                image.size = NSSize(width: 22, height: 22)
                button.image = image
            } else {
                // Fallback: text-based icon
                button.title = "VAF"
            }
            button.toolTip = "VAF Agent Framework"
        }
        
        // Create Menu
        let menu = NSMenu()
        
        statusMenuItem = NSMenuItem(title: "Status: Starting...", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        
        menu.addItem(NSMenuItem.separator())
        
        let openItem = NSMenuItem(title: "Open WebUI", action: #selector(openWebUI), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)
        
        menu.addItem(NSMenuItem.separator())
        
        let quitItem = NSMenuItem(title: "Quit VAF", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        
        statusItem.menu = menu
        
        // Start VAF Backend + Frontend
        startVAF()
        
        // Start status check timer
        checkTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.checkStatus()
        }
    }
    
    func startVAF() {
        // Check if backend is already running
        let checkTask = Process()
        checkTask.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        checkTask.arguments = ["-i", ":8001", "-sTCP:LISTEN", "-t"]
        let checkPipe = Pipe()
        checkTask.standardOutput = checkPipe
        checkTask.standardError = Pipe()
        
        do {
            try checkTask.run()
            checkTask.waitUntilExit()
            let data = checkPipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            
            if !output.isEmpty {
                // Backend already running, just open browser
                NSLog("[VAF] Backend already running, opening browser")
                statusMenuItem.title = "Status: Running"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                    self.openWebUI()
                }
                return
            }
        } catch {
            NSLog("[VAF] Error checking backend: \(error)")
        }
        
        // Start VAF via run_vaf.sh (without tray - just backend + frontend)
        let runScript = vafDir + "/run_vaf.sh"
        
        guard FileManager.default.fileExists(atPath: runScript) else {
            NSLog("[VAF] run_vaf.sh not found at \(runScript)")
            statusMenuItem.title = "Status: Error - Script not found"
            return
        }
        
        vafProcess = Process()
        vafProcess?.executableURL = URL(fileURLWithPath: "/bin/bash")
        vafProcess?.arguments = ["-l", runScript, "tray"]
        vafProcess?.currentDirectoryURL = URL(fileURLWithPath: vafDir)
        
        // Set up environment - source NVM
        var env = ProcessInfo.processInfo.environment
        let nvmDir = NSHomeDirectory() + "/.nvm"
        if FileManager.default.fileExists(atPath: nvmDir + "/nvm.sh") {
            // Add NVM node paths
            let nodeVersionsDir = nvmDir + "/versions/node"
            if let contents = try? FileManager.default.contentsOfDirectory(atPath: nodeVersionsDir),
               let latestNode = contents.sorted().last {
                let nodeBin = nodeVersionsDir + "/" + latestNode + "/bin"
                env["PATH"] = nodeBin + ":" + (env["PATH"] ?? "")
                env["NVM_DIR"] = nvmDir
            }
        }
        env["VAF_NATIVE_WRAPPER"] = "1"  // Signal to Python to skip its own tray icon
        env["VAF_SKIP_TRAY"] = "1"  // Extra signal
        vafProcess?.environment = env
        
        // Redirect output to log
        let logPath = vafDir + "/logs/native_wrapper.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        if let logHandle = FileHandle(forWritingAtPath: logPath) {
            vafProcess?.standardOutput = logHandle
            vafProcess?.standardError = logHandle
        }
        
        do {
            try vafProcess?.run()
            NSLog("[VAF] Started run_vaf.sh (PID: \(vafProcess?.processIdentifier ?? -1))")
            statusMenuItem.title = "Status: Starting..."
            
            // Open browser after delay
            DispatchQueue.main.asyncAfter(deadline: .now() + 8.0) {
                self.openWebUI()
            }
        } catch {
            NSLog("[VAF] Failed to start: \(error)")
            statusMenuItem.title = "Status: Error - \(error.localizedDescription)"
        }
    }
    
    func checkStatus() {
        // Check if backend is responding
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-i", ":8001", "-sTCP:LISTEN", "-t"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        
        do {
            try task.run()
            task.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            
            DispatchQueue.main.async {
                if !output.isEmpty {
                    self.statusMenuItem.title = "Status: Running ✅"
                    // Update icon to active
                    let iconPath = NSHomeDirectory() + "/.vaf/icons/tray_v2_idle.png"
                    if let image = NSImage(contentsOfFile: iconPath) {
                        image.isTemplate = true
                        image.size = NSSize(width: 18, height: 18)
                        self.statusItem.button?.image = image
                    }
                } else if self.vafProcess?.isRunning == true {
                    self.statusMenuItem.title = "Status: Starting..."
                } else {
                    self.statusMenuItem.title = "Status: Stopped"
                }
            }
        } catch {
            NSLog("[VAF] Status check error: \(error)")
        }
    }
    
    @objc func openWebUI() {
        // Read frontend port from file
        let portFile = vafDir + "/vaf/data/frontend_port.txt"
        if let portStr = try? String(contentsOfFile: portFile, encoding: .utf8) {
            frontendPort = portStr.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        
        let url = URL(string: "http://localhost:\(frontendPort)")!
        NSWorkspace.shared.open(url)
    }
    
    @objc func quitApp() {
        NSLog("[VAF] Quit requested - killing all VAF processes...")
        
        // Stop the timer
        checkTimer?.invalidate()
        
        // Kill VAF process
        if let proc = vafProcess, proc.isRunning {
            proc.terminate()
        }
        
        // Kill all Node.js VAF processes
        let killNode = Process()
        killNode.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killNode.arguments = ["-9", "-f", "node.*VAF"]
        killNode.standardOutput = Pipe()
        killNode.standardError = Pipe()
        try? killNode.run()
        killNode.waitUntilExit()
        
        // Kill all Python VAF processes
        let killPython = Process()
        killPython.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killPython.arguments = ["-9", "-f", "python.*vaf"]
        killPython.standardOutput = Pipe()
        killPython.standardError = Pipe()
        try? killPython.run()
        killPython.waitUntilExit()
        
        // Kill processes on ports
        for port in ["3000", "3001", "8001"] {
            let lsof = Process()
            lsof.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
            lsof.arguments = ["-ti:\(port)"]
            let pipe = Pipe()
            lsof.standardOutput = pipe
            lsof.standardError = Pipe()
            try? lsof.run()
            lsof.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            if let pids = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               !pids.isEmpty {
                for pid in pids.components(separatedBy: "\n") {
                    if let pidInt = Int32(pid.trimmingCharacters(in: .whitespaces)) {
                        kill(pidInt, SIGKILL)
                    }
                }
            }
        }
        
        NSLog("[VAF] All processes killed. Exiting.")
        NSApplication.shared.terminate(nil)
    }
    
    func applicationWillTerminate(_ notification: Notification) {
        quitApp()
    }
}

// Main entry point
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

// CRITICAL: Set activation policy BEFORE running
// .accessory = menubar only (no Dock icon)
// .regular = Dock icon + menubar
app.setActivationPolicy(.regular)

app.run()
