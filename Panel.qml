import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Itsycal-style calendar popout. BarWidget.qml remains the visible clock and
// this nested panel keeps the stock open/close/owner contracts used by the bar.
Panel {
  id: root
  moduleName: "elijahrou.calendar"
  ipcTarget: "elijahrou.calendar"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  property date today: new Date()
  property int viewYear: today.getFullYear()
  property int viewMonth: today.getMonth()
  property string selectedKey: Model.keyForDate(today)
  readonly property string todayKey: Model.keyForDate(today)
  readonly property date viewDate: new Date(viewYear, viewMonth, 1)
  readonly property int weekStart: Model.normalizedWeekStart(setting("weekStartDay", null), Qt.locale().firstDayOfWeek)
  readonly property var weekdays: Model.weekdayOrder(weekStart)
  readonly property var weeks: Model.monthGrid(viewYear, viewMonth, weekStart, todayKey)

  property var events: []
  readonly property var eventsByDate: Model.mapEventsByDate(events)
  readonly property var agendaEvents: Model.upcomingEvents(events, selectedKey, 8)
  property var calendars: []
  readonly property var calendarOptions: calendarOptionsFor(calendars)
  property var remoteAccount: ({ connected: false, provider: "", displayName: "", setupMode: "connect" })

  property bool listLoading: false
  property bool calendarsLoading: false
  property bool createLoading: false
  property string listError: ""
  property string calendarError: ""
  property string backendStatus: ""
  property bool addingEvent: false
  property string formError: ""
  property bool addingAccount: false
  property string setupState: "idle"
  property string setupStage: ""
  property string setupMessage: ""
  property string setupError: ""
  property string setupWarning: ""
  property string setupBrowserUrl: ""
  property bool setupCancelled: false
  property bool setupTimedOut: false
  property bool setupProtocolError: false
  property int setupSequence: 0
  property string activeSetupRequestId: ""
  property var setupFinalResponse: null
  property int setupResponseCharacters: 0
  property int setupResponseLines: 0
  property bool setupReplacesExisting: false
  property string setupClientFilePath: ""
  property bool googleClientPickerOpen: false

  property int requestSequence: 0
  property var requestQueue: []
  property var activeRequest: null
  property string latestListRequestId: ""
  property string latestCalendarsRequestId: ""
  readonly property int maxResponseBytes: 1048576
  readonly property int maxQueuedRequests: 8
  readonly property int maxSetupResponseCharacters: 65536
  readonly property int maxSetupResponseLines: 64
  readonly property bool requestBusy: requestProcess.running || root.activeRequest !== null || root.requestQueue.length > 0
  readonly property bool setupBusy: setupProcess.running || root.setupState === "running" || root.setupState === "cancelling"
  readonly property bool anyOperationBusy: root.requestBusy || root.setupBusy
  readonly property string helperPath: Model.localPathForUrl(Qt.resolvedUrl("bin/omarchy-calendar"))
  readonly property string googleCloudSetupUrl: "https://console.cloud.google.com/auth/clients"

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int cellWidth: Style.space(51)
  readonly property int cellHeight: Style.space(38)
  readonly property int cellSpacing: Style.space(2)

  function open() {
    if (root.googleClientPickerOpen) return
    refresh()
    root.controller.show()
    Qt.callLater(function() {
      if (root.opened) root.setCenterHoverRevealSuppressed(true)
    })
  }

  function close() {
    // KeyboardPanel dismisses outside clicks. A native file dialog lives
    // outside that surface, so preserve this form while it owns interaction.
    if (root.googleClientPickerOpen) return
    if (root.setupBusy) {
      root.cancelAccountSetup()
      return
    }
    root.setCenterHoverRevealSuppressed(false)
    root.cancelAdd()
    root.closeSetupForm()
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function setCenterHoverRevealSuppressed(value) {
    if (root.bar && "centerHoverRevealSuppressed" in root.bar)
      root.bar.centerHoverRevealSuppressed = value
  }

  function refresh() {
    if (root.setupBusy) return
    root.today = new Date()
    root.viewYear = root.today.getFullYear()
    root.viewMonth = root.today.getMonth()
    root.selectedKey = Model.keyForDate(root.today)
    root.refreshData(true)
  }

  function refreshData(includeMetadata) {
    root.requestEventRange()
    if (includeMetadata || root.calendars.length === 0) {
      root.calendarsLoading = true
      root.calendarError = ""
      root.latestCalendarsRequestId = root.enqueueRequest({ action: "calendars" })
      root.enqueueRequest({ action: "status" })
    }
  }

  function requestEventRange() {
    if (!root.weeks || root.weeks.length !== 6) return
    var first = root.weeks[0].days[0].key
    var lastCell = root.weeks[5].days[6]
    root.listLoading = true
    root.listError = ""
    root.events = []
    root.latestListRequestId = root.enqueueRequest({
      action: "list",
      start: first,
      end: lastCell.key
    })
  }

  function enqueueRequest(request) {
    if (root.setupBusy) {
      root.failAction(String(request.action || ""), "Account setup is in progress")
      return ""
    }
    if (root.helperPath === "") {
      root.failAction(String(request.action || ""), "Calendar helper path is unavailable")
      return ""
    }
    if (root.requestQueue.length >= root.maxQueuedRequests) {
      root.failAction(String(request.action || ""), "Too many calendar requests")
      return ""
    }
    root.requestSequence++
    var copy = {}
    for (var key in request) copy[key] = request[key]
    copy.requestId = "qml-" + root.requestSequence
    var next = root.requestQueue.slice()
    next.push(copy)
    root.requestQueue = next
    root.startNextRequest()
    return copy.requestId
  }

  function startNextRequest() {
    if (root.setupBusy || requestProcess.running || root.activeRequest || root.requestQueue.length === 0) return
    var queue = root.requestQueue.slice()
    root.activeRequest = queue.shift()
    root.requestQueue = queue
    requestProcess.responseText = ""
    requestProcess.errorText = ""
    requestProcess.timedOut = false
    requestProcess.command = [root.helperPath, "request"]
    requestProcess.running = true
  }

  function finishRequest(exitCode) {
    requestTimeout.stop()
    requestHardKill.stop()
    var request = root.activeRequest
    root.activeRequest = null
    if (!request) {
      root.startNextRequest()
      return
    }
    var action = String(request.action || "")
    if (requestProcess.timedOut) {
      root.failAction(action, "Calendar request timed out")
      root.startNextRequest()
      return
    }
    var output = String(requestProcess.responseText || "")
    if (output.length > root.maxResponseBytes) {
      root.failAction(action, "Calendar response was too large")
      root.startNextRequest()
      return
    }
    if (exitCode !== 0) {
      var stderr = String(requestProcess.errorText || "").trim()
      root.failAction(action, stderr || "Calendar helper failed")
      root.startNextRequest()
      return
    }

    var response = null
    try { response = JSON.parse(output) }
    catch (error) {
      root.failAction(action, "Calendar helper returned invalid JSON")
      root.startNextRequest()
      return
    }
    if (!response || typeof response !== "object" || Array.isArray(response)) {
      root.failAction(action, "Calendar helper returned an invalid response")
      root.startNextRequest()
      return
    }
    if (response.ok === false || response.error) {
      var message = response.error && response.error.message ? response.error.message : response.error
      root.failAction(action, String(message || "Calendar request failed"))
      root.startNextRequest()
      return
    }
    if (String(response.requestId || "") !== String(request.requestId)) {
      root.failAction(action, "Calendar response did not match its request")
      root.startNextRequest()
      return
    }
    var body = response.result !== undefined ? response.result
      : (response.data !== undefined ? response.data : response)
    root.applyResponse(action, request.requestId, body)
    root.startNextRequest()
  }

  function failAction(action, message) {
    if (action === "list") {
      root.listLoading = false
      root.listError = message
    } else if (action === "calendars") {
      root.calendarsLoading = false
      root.calendarError = message
    } else if (action === "create") {
      root.createLoading = false
      root.formError = message
    } else if (action === "status") {
      root.backendStatus = message
    }
  }

  function applyResponse(action, requestId, body) {
    if (action === "list") {
      if (requestId !== root.latestListRequestId) return
      var rawEvents = Array.isArray(body) ? body : (body && Array.isArray(body.events) ? body.events : [])
      root.events = Model.normalizeEvents(rawEvents)
      root.listLoading = false
      root.listError = ""
    } else if (action === "calendars") {
      if (requestId !== root.latestCalendarsRequestId) return
      var rawCalendars = Array.isArray(body) ? body : (body && Array.isArray(body.calendars) ? body.calendars : [])
      var clean = []
      for (var i = 0; i < rawCalendars.length && clean.length < 100; i++) {
        var calendar = rawCalendars[i]
        if (!calendar || typeof calendar !== "object") continue
        var id = String(calendar.id || calendar.calendarId || "").trim()
        var name = String(calendar.name || calendar.calendarName || id).trim()
        if (id !== "") clean.push({ id: id.substr(0, 200), name: name.substr(0, 200) })
      }
      root.calendars = clean
      root.calendarsLoading = false
      root.calendarError = clean.length === 0 ? "No writable calendars" : ""
    } else if (action === "status") {
      var account = body && body.remoteAccount && typeof body.remoteAccount === "object"
        ? body.remoteAccount : {}
      root.remoteAccount = {
        connected: account.connected === true,
        provider: String(account.provider || "").substr(0, 16),
        displayName: String(account.displayName || "").substr(0, 256),
        setupMode: account.setupMode === "replace" ? "replace" : "connect"
      }
      if (body && (body.configured === false || body.ready === false))
        root.backendStatus = String(body.message || "Calendar setup is required")
      else root.backendStatus = body && body.error ? String(body.error) : ""
    } else if (action === "create") {
      root.createLoading = false
      root.addingEvent = false
      root.formError = ""
      root.requestEventRange()
    }
  }

  function calendarOptionsFor(values) {
    var options = []
    for (var i = 0; i < values.length; i++) options.push({ value: values[i].id, label: values[i].name })
    return options
  }

  function moveMonth(delta) {
    var next = Model.stepMonth(root.viewYear, root.viewMonth, delta)
    root.viewYear = next.year
    root.viewMonth = next.month
    root.selectedKey = Model.dateKey(next.year, next.month, 1)
    root.requestEventRange()
  }

  function selectDay(day) {
    root.selectedKey = day.key
    if (!day.inMonth) {
      root.viewYear = day.year
      root.viewMonth = day.month
      root.requestEventRange()
    }
  }

  function moveSelection(days) {
    var parts = root.selectedKey.split("-")
    if (parts.length !== 3) return
    var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]) + days)
    var monthChanged = date.getFullYear() !== root.viewYear || date.getMonth() !== root.viewMonth
    root.selectedKey = Model.keyForDate(date)
    if (monthChanged) {
      root.viewYear = date.getFullYear()
      root.viewMonth = date.getMonth()
      root.requestEventRange()
    }
  }

  function toggleWeekStart() {
    var next = Model.toggledWeekStart(root.weekStart)
    var entry = { id: root.moduleName }
    for (var key in root.settings) if (key !== "id") entry[key] = root.settings[key]
    entry.weekStartDay = Model.weekStartSettingName(next)
    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget) root.hostWidget.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function weekdayLabel(day) {
    return String(Qt.locale().dayName(day, Locale.ShortFormat)).replace(/\.$/, "").toUpperCase()
  }

  function startAdd() {
    if (root.setupBusy) return
    root.closeSetupForm()
    root.addingEvent = true
    root.formError = ""
    eventCalendar.value = root.calendars.length > 0 ? root.calendars[0].id : ""
    eventTitle.text = ""
    eventDate.text = root.selectedKey
    eventStart.text = "09:00"
    eventEnd.text = "10:00"
    Qt.callLater(function() { eventTitle.forceActiveFocus() })
  }

  function cancelAdd() {
    if (root.createLoading) return
    root.addingEvent = false
    root.createLoading = false
    root.formError = ""
    if (eventCalendar.popupOpen) eventCalendar.close()
    Qt.callLater(function() { if (keyCatcher) keyCatcher.forceActiveFocus() })
  }

  function submitAdd() {
    if (root.createLoading || root.setupBusy) return
    var checked = Model.validateCreateInput({
      calendarId: eventCalendar.value,
      title: eventTitle.text,
      date: eventDate.text,
      start: eventStart.text,
      end: eventEnd.text
    })
    if (!checked.valid) {
      root.formError = checked.error
      return
    }
    root.formError = ""
    root.createLoading = true
    var request = { action: "create" }
    for (var key in checked.value) request[key] = checked.value[key]
    root.enqueueRequest(request)
  }

  function startAccountSetup() {
    if (root.createLoading || root.setupBusy) return
    root.cancelAdd()
    root.addingAccount = true
    root.setupState = "idle"
    root.setupStage = ""
    root.setupMessage = ""
    root.setupError = ""
    root.setupWarning = ""
    root.setupBrowserUrl = ""
    root.setupReplacesExisting = root.remoteAccount.setupMode === "replace"
    setupProvider.value = "google"
    setupDisplayName.text = ""
    setupUsername.text = ""
    setupUrl.text = ""
    setupClientId.text = ""
    setupSecret.text = ""
    root.setupClientFilePath = ""
    Qt.callLater(function() { setupProvider.forceActiveFocus() })
  }

  function clearSetupSecret() {
    setupSecret.text = ""
    setupProcess.requestText = ""
  }

  function openGoogleClientFilePicker() {
    if (root.setupBusy || googleClientPickerProcess.running) return
    var home = Quickshell.env("HOME") || ""
    var command = [
      "/usr/bin/zenity",
      "--file-selection",
      "--title=Import Google Desktop OAuth JSON",
      "--file-filter=JSON files | *.json"
    ]
    if (home !== "") command.push("--filename=" + home + "/Downloads/")
    root.googleClientPickerOpen = true
    googleClientPickerProcess.outputText = ""
    googleClientPickerProcess.command = command
    root.setCenterHoverRevealSuppressed(false)
    root.controller.hide()
    googleClientPickerProcess.running = true
  }

  function finishGoogleClientFilePicker(exitCode) {
    root.googleClientPickerOpen = false
    var output = googleClientPickerProcess.outputText
    googleClientPickerProcess.outputText = ""
    if (exitCode === 0) {
      if (output.length > 4097) {
        root.setupError = "Selected credential path is too long"
      } else {
        root.acceptGoogleClientFile(output.replace(/\r?\n$/, ""))
      }
    } else if (exitCode !== 1) {
      root.setupError = "Unable to open the external file picker"
    }
    root.controller.show()
    Qt.callLater(function() {
      if (!root.opened) return
      root.setCenterHoverRevealSuppressed(true)
      if (keyCatcher) keyCatcher.forceActiveFocus()
    })
  }

  function acceptGoogleClientFile(fileUrl) {
    if (root.setupBusy || setupProvider.value !== "google") return
    var path = Model.localPathForUrl(String(fileUrl || ""))
    if (path === "") {
      root.setupError = "Choose one local JSON file"
      return
    }
    setupClientId.text = ""
    setupSecret.text = ""
    root.resetSetupFeedback()
    root.setupClientFilePath = path
    root.setupMessage = "Desktop OAuth JSON selected. Click Connect to continue."
  }

  function closeSetupForm() {
    if (root.setupBusy) return
    root.clearSetupSecret()
    root.setupClientFilePath = ""
    root.addingAccount = false
    root.setupState = "idle"
    root.setupStage = ""
    root.setupMessage = ""
    root.setupError = ""
    root.setupWarning = ""
    root.setupBrowserUrl = ""
    if (setupProvider.popupOpen) setupProvider.close()
    Qt.callLater(function() { if (keyCatcher) keyCatcher.forceActiveFocus() })
  }

  function resetSetupFeedback() {
    if (root.setupBusy) return
    root.setupState = "idle"
    root.setupStage = ""
    root.setupMessage = ""
    root.setupError = ""
    root.setupWarning = ""
    root.setupBrowserUrl = ""
  }

  function submitAccountSetup() {
    if (root.setupBusy) return
    if (root.requestBusy) {
      root.setupError = "Wait for the current calendar operation to finish"
      return
    }
    if (root.helperPath === "") {
      root.setupError = "Calendar helper path is unavailable"
      return
    }
    var checked = Model.validateSetupInput({
      provider: setupProvider.value,
      displayName: setupDisplayName.text,
      username: setupUsername.text,
      url: setupUrl.text,
      clientId: setupClientId.text,
      clientFile: root.setupClientFilePath,
      secret: setupSecret.text
    })
    if (!checked.valid) {
      root.setupError = checked.error
      return
    }

    root.setupSequence++
    root.activeSetupRequestId = "setup-qml-" + root.setupSequence
    checked.value.requestId = root.activeSetupRequestId
    setupProcess.requestText = JSON.stringify(checked.value)
    checked.value.secret = ""
    root.setupState = "running"
    root.setupStage = "starting"
    root.setupMessage = "Starting account setup…"
    root.setupError = ""
    root.setupWarning = ""
    root.setupBrowserUrl = ""
    root.setupCancelled = false
    root.setupTimedOut = false
    root.setupProtocolError = false
    root.setupFinalResponse = null
    root.setupResponseCharacters = 0
    root.setupResponseLines = 0
    root.setupReplacesExisting = root.remoteAccount.setupMode === "replace"
    setupProcess.command = [root.helperPath, "setup-request"]
    setupProcess.running = true
  }

  function failSetupProtocol(message) {
    root.setupProtocolError = true
    root.setupError = message
    if (setupProcess.running) setupProcess.signal(15)
    setupHardKill.restart()
  }

  function acceptSetupLine(data) {
    if (!root.setupBusy || root.setupProtocolError) return
    var line = String(data || "")
    root.setupResponseCharacters += line.length + 1
    root.setupResponseLines++
    if (line.length === 0 || line.length > 16384
        || root.setupResponseCharacters > root.maxSetupResponseCharacters
        || root.setupResponseLines > root.maxSetupResponseLines) {
      root.failSetupProtocol("Calendar setup returned too much data")
      return
    }
    var parsed = Model.parseSetupProtocolLine(line, root.activeSetupRequestId, {
      browserSeen: root.setupBrowserUrl !== "",
      finalSeen: root.setupFinalResponse !== null
    })
    if (!parsed.valid) {
      root.failSetupProtocol(parsed.error || "Calendar setup returned invalid data")
      return
    }
    var response = parsed.response
    if (parsed.kind === "progress") {
      root.setupStage = String(response.stage || "").substr(0, 32)
      root.setupMessage = String(response.message || "Working…").substr(0, 500)
      if (response.replacesExisting === true) root.setupReplacesExisting = true
    } else if (parsed.kind === "browser") {
      root.setupBrowserUrl = String(response.url)
      root.setupMessage = "Complete authorization in your browser"
      Qt.openUrlExternally(root.setupBrowserUrl)
    } else if (parsed.kind === "result") {
      root.setupFinalResponse = {
        ok: response.ok === true,
        replacesExisting: response.replacesExisting === true,
        cleanupComplete: response.cleanupComplete !== false,
        errorMessage: response.error && response.error.message
          ? String(response.error.message).substr(0, 500) : ""
      }
      if (response.replacesExisting === true) root.setupReplacesExisting = true
      root.setupMessage = response.ok === true ? "Finishing setup…" : ""
    }
  }

  function cancelAccountSetup() {
    if (!root.setupBusy) {
      root.closeSetupForm()
      return
    }
    if (!setupProcess.running || root.setupStage === "committing" || root.setupState === "cancelling") return
    root.setupCancelled = true
    root.setupState = "cancelling"
    root.setupMessage = "Cancelling account setup…"
    setupProcess.signal(15)
    setupHardKill.restart()
  }

  function finishAccountSetup(exitCode) {
    setupWatchdog.stop()
    setupHardKill.stop()
    root.clearSetupSecret()
    var finalResponse = root.setupFinalResponse
    root.setupFinalResponse = null

    if (root.setupProtocolError) {
      root.setupState = "error"
      root.setupMessage = ""
      if (root.setupError === "") root.setupError = "Calendar setup returned invalid data"
      return
    }
    var presentation = Model.setupResultPresentation(finalResponse, root.setupCancelled)
    if (presentation !== null) {
      root.setupState = presentation.state
      root.setupMessage = presentation.message
      root.setupWarning = presentation.warning
      root.setupError = ""
      if (presentation.state === "success") root.refreshData(true)
      return
    }
    if (root.setupTimedOut) {
      root.setupState = "error"
      root.setupMessage = ""
      root.setupError = "Account setup timed out after 11 minutes"
      return
    }
    if (exitCode !== 0 || finalResponse === null) {
      root.setupState = "error"
      root.setupMessage = ""
      root.setupError = exitCode !== 0 ? "Calendar setup helper failed" : "Calendar setup ended without a result"
      return
    }
    root.setupState = "error"
    root.setupMessage = ""
    root.setupError = finalResponse.errorMessage || "Account setup failed"
  }

  function eventTime(event) {
    if (event.allDay) return "ALL DAY"
    var start = new Date(event.start)
    var end = new Date(event.end)
    if (!isFinite(start.getTime()) || !isFinite(end.getTime())) return ""
    return Qt.formatTime(start, "HH:mm") + "–" + Qt.formatTime(end, "HH:mm")
  }

  Process {
    id: googleClientPickerProcess
    property string outputText: ""

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: googleClientPickerProcess.outputText = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: function() { /* Deliberately discard picker diagnostics. */ }
    }
    onExited: function(exitCode) {
      Qt.callLater(function() { root.finishGoogleClientFilePicker(exitCode) })
    }
  }

  Process {
    id: requestProcess
    property string responseText: ""
    property string errorText: ""
    property bool timedOut: false
    stdinEnabled: true

    onStarted: {
      requestTimeout.restart()
      write(JSON.stringify(root.activeRequest) + "\n")
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: requestProcess.responseText = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: requestProcess.errorText = String(text || "")
    }
    onExited: function(exitCode) {
      Qt.callLater(function() { root.finishRequest(exitCode) })
    }
  }

  Timer {
    id: requestTimeout
    interval: 45000
    repeat: false
    onTriggered: {
      if (!requestProcess.running) return
      requestProcess.timedOut = true
      requestProcess.signal(15)
      requestHardKill.restart()
    }
  }

  Timer {
    id: requestHardKill
    interval: 1500
    repeat: false
    onTriggered: if (requestProcess.running) requestProcess.signal(9)
  }

  Process {
    id: setupProcess
    property string requestText: ""
    stdinEnabled: true

    onStarted: {
      setupWatchdog.restart()
      var outgoing = setupProcess.requestText
      write(outgoing + "\n")
      outgoing = ""
      root.clearSetupSecret()
      root.setupClientFilePath = ""
    }
    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { root.acceptSetupLine(data) }
    }
    stderr: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { /* Deliberately discard helper diagnostics. */ }
    }
    onExited: function(exitCode) {
      Qt.callLater(function() { root.finishAccountSetup(exitCode) })
    }
  }

  Timer {
    id: setupWatchdog
    interval: 660000
    repeat: false
    onTriggered: {
      if (!setupProcess.running) return
      root.setupTimedOut = true
      root.setupState = "cancelling"
      root.setupMessage = "Stopping account setup…"
      setupProcess.signal(15)
      setupHardKill.restart()
    }
  }

  Timer {
    id: setupHardKill
    interval: 30000
    repeat: false
    onTriggered: if (setupProcess.running) setupProcess.signal(9)
  }

  SystemClock {
    id: clock
    precision: SystemClock.Minutes
    onDateChanged: {
      if (Model.keyForDate(clock.date) === root.todayKey) return
      root.today = clock.date
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(480))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.addingEvent || root.addingAccount || eventCalendar.popupOpen || setupProvider.popupOpen
      onMoveRequested: function(dx, dy) { root.moveSelection(dx + dy * 7) }
      onActivateRequested: root.selectedKey = root.todayKey
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "[") root.moveMonth(-1)
        else if (text === "]") root.moveMonth(1)
        else if (text === "t" || text === "T") {
          root.viewYear = root.today.getFullYear()
          root.viewMonth = root.today.getMonth()
          root.selectedKey = root.todayKey
          root.requestEventRange()
        } else if (text === "a" || text === "A") root.startAdd()
        else if (text === "c" || text === "C") root.startAccountSetup()
        else if (text === "r" || text === "R") root.refreshData(true)
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: contentColumn
          width: parent.width
          spacing: Style.space(10)

          Item {
            width: parent.width
            height: monthNavigation.implicitHeight

            Row {
              id: monthNavigation
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(12)

              Button {
                iconText: "󰅁"
                tooltipText: "Previous month"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                focusable: true
                onClicked: root.moveMonth(-1)
              }
              Text {
                width: Style.space(230)
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignHCenter
                text: Qt.formatDate(root.viewDate, "MMMM yyyy").toUpperCase()
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                font.letterSpacing: 1
              }
              Button {
                iconText: "󰅂"
                tooltipText: "Next month"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                focusable: true
                onClicked: root.moveMonth(1)
              }
            }

            Button {
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰒓"
              tooltipText: "Calendar settings"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              bordered: false
              focusable: true
              enabled: !root.anyOperationBusy && !root.addingAccount && !root.addingEvent
              onClicked: root.startAccountSetup()
            }
          }

          Column {
            id: monthGrid
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.cellSpacing

            Row {
              spacing: root.cellSpacing
              Repeater {
                model: root.weekdays
                Text {
                  required property var modelData
                  width: root.cellWidth
                  height: Style.space(18)
                  horizontalAlignment: Text.AlignHCenter
                  text: root.weekdayLabel(modelData)
                  color: Qt.darker(root.contentForeground, 1.5)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }
            }

            Repeater {
              model: root.weeks
              Row {
                required property var modelData
                spacing: root.cellSpacing
                Repeater {
                  model: modelData.days
                  Rectangle {
                    id: dayCell
                    required property var modelData
                    readonly property var dayEvents: root.eventsByDate[modelData.key] || []
                    width: root.cellWidth
                    height: root.cellHeight
                    radius: Style.cornerRadius
                    color: modelData.key === root.selectedKey
                      ? Style.selectedFillFor(root.contentForeground, Color.accent) : "transparent"
                    border.width: modelData.today || modelData.key === root.selectedKey ? Style.spacing.hairline : 0
                    border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

                    Text {
                      anchors.horizontalCenter: parent.horizontalCenter
                      y: Style.space(3)
                      text: dayCell.modelData.day
                      color: dayCell.modelData.inMonth ? root.contentForeground : Qt.darker(root.contentForeground, 2.1)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.body
                      font.bold: dayCell.modelData.today || dayCell.modelData.key === root.selectedKey
                    }
                    Row {
                      anchors.horizontalCenter: parent.horizontalCenter
                      anchors.bottom: parent.bottom
                      anchors.bottomMargin: Style.space(4)
                      spacing: Style.space(2)
                      Repeater {
                        model: Math.min(3, dayCell.dayEvents.length)
                        Rectangle {
                          required property int index
                          width: Style.space(4)
                          height: width
                          radius: width / 2
                          color: dayCell.dayEvents[index].color
                        }
                      }
                    }
                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.selectDay(dayCell.modelData)
                    }
                  }
                }
              }
            }
          }

          Item {
            width: monthGrid.width
            height: upcomingHeading.implicitHeight
            anchors.horizontalCenter: parent.horizontalCenter

            Text {
              id: upcomingHeading
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "UPCOMING FROM " + root.selectedKey
              color: Qt.darker(root.contentForeground, 1.35)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1
            }

            Button {
              id: addEventButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: "+"
              tooltipText: "Add event"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              bordered: false
              focusable: true
              enabled: !root.anyOperationBusy && !root.addingAccount && !root.addingEvent
                && !root.calendarsLoading && root.calendars.length > 0
              onClicked: root.startAdd()
            }
          }

          Text {
            visible: root.listLoading
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            text: "Loading events…"
            color: Qt.darker(root.contentForeground, 1.4)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.body
          }

          Column {
            visible: !root.listLoading && root.listError !== ""
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(4)
            Text {
              width: parent.width
              text: root.listError
              wrapMode: Text.Wrap
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
            }
            Button {
              text: "Retry"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              bordered: true
              focusable: true
              onClicked: root.requestEventRange()
            }
          }

          Text {
            visible: !root.listLoading && root.listError === "" && root.agendaEvents.length === 0
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            text: "No upcoming events in this view"
            color: Qt.darker(root.contentForeground, 1.5)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.body
          }

          Column {
            visible: !root.listLoading && root.listError === ""
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(5)
            Repeater {
              model: root.agendaEvents
              Rectangle {
                required property var modelData
                width: parent.width
                height: agendaRow.implicitHeight + Style.space(10)
                radius: Style.cornerRadius
                color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.055)
                Rectangle {
                  width: Style.space(4)
                  height: parent.height
                  radius: parent.radius
                  color: modelData.color
                }
                Row {
                  id: agendaRow
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(8)
                  spacing: Style.space(8)
                  Text {
                    width: Style.space(92)
                    text: modelData.startKey + "\n" + root.eventTime(modelData)
                    color: Qt.darker(root.contentForeground, 1.35)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    width: parent.width - Style.space(100)
                    text: modelData.title + "\n" + modelData.calendarName
                    elide: Text.ElideRight
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.body
                  }
                }
              }
            }
          }

          Column {
            visible: root.addingAccount
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)
            Keys.onEscapePressed: {
              if (root.setupBusy) root.cancelAccountSetup()
              else root.closeSetupForm()
            }

            Text {
              text: "ADD CALENDAR ACCOUNT"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              font.letterSpacing: 1
            }
            Text {
              width: parent.width
              text: root.setupReplacesExisting || root.remoteAccount.setupMode === "replace"
                ? "This setup replaces the existing remote account"
                  + (root.remoteAccount.displayName !== "" ? " (“" + root.remoteAccount.displayName + "”)" : "")
                  + ". Only one remote account is supported; this does not add a second account."
                : "Connect one remote calendar account."
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: root.setupReplacesExisting || root.remoteAccount.setupMode === "replace"
                ? Color.urgent : Qt.darker(root.contentForeground, 1.35)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Dropdown {
              id: setupProvider
              width: parent.width
              label: "Provider"
              value: "google"
              options: [
                { value: "google", label: "Google" },
                { value: "icloud", label: "iCloud" },
                { value: "caldav", label: "Generic CalDAV" }
              ]
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              enabled: !root.setupBusy
              onChanged: function(value) {
                setupSecret.text = ""
                root.setupClientFilePath = ""
                root.resetSetupFeedback()
              }
            }
            Text {
              width: parent.width
              text: setupProvider.value === "google"
                ? "Create your own Google Desktop OAuth client, import its downloaded JSON, then authorize calendar access."
                : (setupProvider.value === "icloud"
                  ? "iCloud requires your Apple ID and an app-specific password, not your Apple ID password."
                  : "Enter the CalDAV server URL, username, and app password or account secret.")
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: Qt.darker(root.contentForeground, 1.35)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Column {
              visible: setupProvider.value === "google"
              width: parent.width
              spacing: Style.space(6)

              Text {
                width: parent.width
                text: "1. Open Google Cloud, select a project, enable Google Calendar API, and configure OAuth consent.\n2. Create an OAuth client with application type Desktop app, then download its JSON.\n3. Import that JSON here and click Connect. External testing apps must include your account as a test user."
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Button {
                width: parent.width
                text: "Open Google Cloud setup"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                bordered: true
                focusable: true
                enabled: !root.setupBusy
                onClicked: Qt.openUrlExternally(root.googleCloudSetupUrl)
              }
              Button {
                width: parent.width
                text: "Import Desktop OAuth JSON"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                bordered: true
                focusable: true
                enabled: !root.setupBusy
                onClicked: root.openGoogleClientFilePicker()
              }
              Text {
                visible: root.setupClientFilePath !== ""
                width: parent.width
                text: "Desktop OAuth JSON selected. The backend reads it only during secure setup."
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: Color.accent
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
            TextField {
              id: setupDisplayName
              width: parent.width
              placeholderText: "Display name (optional)"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.setupBusy
              onTextChanged: root.resetSetupFeedback()
            }
            Text {
              visible: setupProvider.value === "google"
              width: parent.width
              text: "Advanced fallback: enter the Desktop client ID and secret manually."
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: Qt.darker(root.contentForeground, 1.35)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }
            TextField {
              id: setupClientId
              visible: setupProvider.value === "google"
              width: parent.width
              placeholderText: "Google Desktop OAuth client ID"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.setupBusy
              inputMethodHints: Qt.ImhNoPredictiveText
              onTextChanged: {
                if (text !== "") root.setupClientFilePath = ""
                root.resetSetupFeedback()
              }
            }
            TextField {
              id: setupUsername
              visible: setupProvider.value !== "google"
              width: parent.width
              placeholderText: setupProvider.value === "icloud" ? "Apple ID" : "CalDAV username"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.setupBusy
              inputMethodHints: Qt.ImhNoPredictiveText
              onTextChanged: root.resetSetupFeedback()
            }
            TextField {
              id: setupUrl
              visible: setupProvider.value === "caldav"
              width: parent.width
              placeholderText: "CalDAV URL (https://…)"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.setupBusy
              inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText
              onTextChanged: root.resetSetupFeedback()
            }
            TextField {
              id: setupSecret
              width: parent.width
              placeholderText: setupProvider.value === "google" ? "OAuth client secret" : "App password / secret"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.setupBusy
              echoMode: TextInput.Password
              inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
              onTextChanged: {
                if (setupProvider.value === "google" && text !== "") root.setupClientFilePath = ""
                root.resetSetupFeedback()
              }
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                  root.submitAccountSetup()
                  event.accepted = true
                }
              }
            }
            Text {
              visible: root.setupMessage !== ""
              width: parent.width
              text: root.setupMessage
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: root.setupState === "success" ? Color.accent : root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              visible: root.setupBrowserUrl !== ""
              width: parent.width
              text: root.setupBrowserUrl
              wrapMode: Text.WrapAnywhere
              textFormat: Text.PlainText
              color: Color.accent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              visible: root.setupWarning !== ""
              width: parent.width
              text: root.setupWarning
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              visible: root.setupError !== ""
              width: parent.width
              text: root.setupError
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Row {
              anchors.right: parent.right
              spacing: Style.space(6)
              Button {
                text: root.setupBusy
                  ? (root.setupStage === "committing" ? "Activating…" : "Cancel setup")
                  : "Close"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                focusable: true
                enabled: !root.setupBusy || root.setupStage !== "committing"
                onClicked: {
                  if (root.setupBusy) root.cancelAccountSetup()
                  else root.closeSetupForm()
                }
              }
              Button {
                visible: !root.setupBusy && root.setupState !== "success"
                text: root.setupState === "error" || root.setupState === "cancelled" ? "Try again" : "Connect"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                bordered: true
                focusable: true
                enabled: !root.requestBusy
                onClicked: root.submitAccountSetup()
              }
            }
          }

          Column {
            visible: root.addingEvent
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)
            Keys.onEscapePressed: root.cancelAdd()

            Text {
              text: "NEW EVENT"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              font.letterSpacing: 1
            }
            Dropdown {
              id: eventCalendar
              width: parent.width
              label: "Calendar"
              value: ""
              options: root.calendarOptions
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onChanged: function(value) { root.formError = "" }
            }
            TextField {
              id: eventTitle
              width: parent.width
              placeholderText: "Title"
              foreground: root.contentForeground
              font.family: root.contentFontFamily
              enabled: !root.createLoading
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) { root.cancelAdd(); event.accepted = true }
                else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.submitAdd(); event.accepted = true }
              }
            }
            Row {
              width: parent.width
              spacing: Style.space(6)
              TextField {
                id: eventDate
                width: parent.width - eventStart.width - eventEnd.width - parent.spacing * 2
                placeholderText: "YYYY-MM-DD"
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                enabled: !root.createLoading
                Keys.onEscapePressed: root.cancelAdd()
              }
              TextField {
                id: eventStart
                width: Style.space(82)
                placeholderText: "09:00"
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                enabled: !root.createLoading
                inputMethodHints: Qt.ImhDigitsOnly
                Keys.onEscapePressed: root.cancelAdd()
              }
              TextField {
                id: eventEnd
                width: Style.space(82)
                placeholderText: "10:00"
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                enabled: !root.createLoading
                inputMethodHints: Qt.ImhDigitsOnly
                Keys.onEscapePressed: root.cancelAdd()
              }
            }
            Text {
              visible: root.formError !== ""
              width: parent.width
              text: root.formError
              wrapMode: Text.Wrap
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Row {
              anchors.right: parent.right
              spacing: Style.space(6)
              Button {
                text: "Cancel"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                focusable: true
                onClicked: root.cancelAdd()
              }
              Button {
                text: root.createLoading ? "Creating…" : "Create"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                bordered: true
                focusable: true
                enabled: !root.createLoading
                onClicked: root.submitAdd()
              }
            }
          }

          Text {
            visible: !root.addingEvent && (root.calendarError !== "" || root.backendStatus !== "")
            width: monthGrid.width
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.calendarError || root.backendStatus
            wrapMode: Text.Wrap
            color: Qt.darker(root.contentForeground, 1.5)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
