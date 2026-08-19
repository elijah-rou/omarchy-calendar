import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Read-only ICS month grid and agenda. Feed credentials cross only the bounded
// subscription process stdin and are cleared from QML immediately after write.
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
  property var subscriptions: []
  property bool listLoading: false
  property string listError: ""
  property string backendStatus: ""
  property bool settingsOpen: false
  property bool addFormOpen: false

  property int requestSequence: 0
  property var requestQueue: []
  property var activeRequest: null
  property string latestListRequestId: ""
  readonly property int maxResponseBytes: 1048576
  readonly property int maxQueuedRequests: 8
  readonly property bool requestBusy: requestProcess.running || activeRequest !== null || requestQueue.length > 0

  property int subscriptionSequence: 0
  property string subscriptionRequestId: ""
  property string subscriptionAction: ""
  property string subscriptionState: "idle"
  property string subscriptionMessage: ""
  property string subscriptionError: ""
  property string pendingSubscriptionPayload: ""
  property var subscriptionProtocolState: ({ finalSeen: false, progressCount: 0 })
  property int subscriptionResponseCharacters: 0
  property int subscriptionResponseLines: 0
  property bool subscriptionTimedOut: false
  property bool subscriptionCancelled: false
  property bool subscriptionCommitStarted: false
  readonly property bool subscriptionBusy: subscriptionProcess.running || subscriptionState === "running" || subscriptionState === "cancelling"

  readonly property string helperPath: Model.localPathForUrl ? Model.localPathForUrl(Qt.resolvedUrl("bin/omarchy-calendar")) : String(Qt.resolvedUrl("bin/omarchy-calendar")).replace(/^file:\/\//, "")
  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int cellWidth: Style.space(51)
  readonly property int cellHeight: Style.space(38)
  readonly property int cellSpacing: Style.space(2)

  function open() {
    refresh()
    controller.show()
    Qt.callLater(function() { if (opened) setCenterHoverRevealSuppressed(true) })
  }
  function close() {
    setCenterHoverRevealSuppressed(false)
    settingsOpen = false
    addFormOpen = false
    clearCredentialFields()
    controller.hide()
  }
  function toggle() { if (opened) close(); else open() }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") return bar.switchPanelFrom(barIdentity, direction)
    return false
  }
  function setCenterHoverRevealSuppressed(value) {
    if (bar && "centerHoverRevealSuppressed" in bar) bar.centerHoverRevealSuppressed = value
  }
  function refresh() {
    today = new Date()
    viewYear = today.getFullYear()
    viewMonth = today.getMonth()
    selectedKey = Model.keyForDate(today)
    refreshData(true)
  }
  function refreshData(includeMetadata) {
    requestEventRange()
    if (includeMetadata) enqueueRequest({ action: "status" })
  }
  function requestEventRange() {
    if (!weeks || weeks.length !== 6) return
    listLoading = true
    listError = ""
    events = []
    latestListRequestId = enqueueRequest({ action: "list", start: weeks[0].days[0].key, end: weeks[5].days[6].key })
  }
  function enqueueRequest(request) {
    if (helperPath === "") { failRequest(String(request.action || ""), "Calendar helper is unavailable"); return "" }
    if (requestQueue.length >= maxQueuedRequests) { failRequest(String(request.action || ""), "Too many calendar requests"); return "" }
    requestSequence++
    var copy = {}
    for (var key in request) copy[key] = request[key]
    copy.requestId = "qml-" + requestSequence
    var next = requestQueue.slice(); next.push(copy); requestQueue = next
    startNextRequest()
    return copy.requestId
  }
  function startNextRequest() {
    if (requestProcess.running || activeRequest || requestQueue.length === 0) return
    var next = requestQueue.slice(); activeRequest = next.shift(); requestQueue = next
    requestProcess.responseText = ""; requestProcess.errorText = ""; requestProcess.timedOut = false
    requestProcess.command = [helperPath, "request"]
    requestProcess.running = true
  }
  function finishRequest(exitCode) {
    requestTimeout.stop(); requestHardKill.stop()
    var request = activeRequest; activeRequest = null
    if (!request) { startNextRequest(); return }
    var action = String(request.action || "")
    var output = String(requestProcess.responseText || "")
    if (requestProcess.timedOut) failRequest(action, "Calendar request timed out")
    else if (output.length > maxResponseBytes) failRequest(action, "Calendar response was too large")
    else if (exitCode !== 0) failRequest(action, String(requestProcess.errorText || "").trim() || "Calendar helper failed")
    else {
      var response = null
      try { response = JSON.parse(output) } catch (error) {}
      if (!response || typeof response !== "object" || Array.isArray(response)) failRequest(action, "Calendar helper returned invalid JSON")
      else if (String(response.requestId || "") !== String(request.requestId)) failRequest(action, "Calendar response did not match its request")
      else if (response.ok === false || response.error) failRequest(action, String(response.error && response.error.message || response.error || "Calendar request failed"))
      else applyResponse(action, request.requestId, response.result !== undefined ? response.result : response)
    }
    startNextRequest()
  }
  function failRequest(action, message) {
    if (action === "list") { listLoading = false; listError = message }
    else backendStatus = message
  }
  function applyResponse(action, requestId, body) {
    if (action === "list") {
      if (requestId !== latestListRequestId) return
      events = Model.normalizeEvents(body && Array.isArray(body.events) ? body.events : [])
      listLoading = false; listError = ""
    } else if (action === "status") {
      subscriptions = Model.normalizeSubscriptionStatus(body && body.subscriptions, body && body.lastRefresh)
      backendStatus = body && body.readOnly === true ? "" : "Calendar backend is not read-only"
    }
  }

  function moveMonth(delta) {
    var next = Model.stepMonth(viewYear, viewMonth, delta)
    viewYear = next.year; viewMonth = next.month; selectedKey = Model.dateKey(next.year, next.month, 1)
    requestEventRange()
  }
  function selectDay(day) {
    selectedKey = day.key
    if (!day.inMonth) { viewYear = day.year; viewMonth = day.month; requestEventRange() }
  }
  function moveSelection(days) {
    var parts = selectedKey.split("-"); if (parts.length !== 3) return
    var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]) + days)
    selectedKey = Model.keyForDate(date)
    if (date.getFullYear() !== viewYear || date.getMonth() !== viewMonth) {
      viewYear = date.getFullYear(); viewMonth = date.getMonth(); requestEventRange()
    }
  }
  function toggleWeekStart() {
    var next = Model.toggledWeekStart(weekStart)
    var entry = { id: moduleName }
    for (var key in settings) if (key !== "id") entry[key] = settings[key]
    entry.weekStartDay = Model.weekStartSettingName(next); settings = entry
    if (hostWidget && "settings" in hostWidget) hostWidget.settings = entry
    if (bar && bar.shell && typeof bar.shell.updateEntryInline === "function") bar.shell.updateEntryInline(moduleName, entry)
  }
  function weekdayLabel(day) { return String(Qt.locale().dayName(day, Locale.ShortFormat)).replace(/\.$/, "").toUpperCase() }
  function eventTime(event) {
    if (event.allDay) return "ALL DAY"
    var start = new Date(event.start); var end = new Date(event.end)
    if (!isFinite(start.getTime()) || !isFinite(end.getTime())) return ""
    return Qt.formatTime(start, "HH:mm") + "–" + Qt.formatTime(end, "HH:mm")
  }

  function openSettings() {
    settingsOpen = true; addFormOpen = false; subscriptionError = ""
    startSubscription({ action: "list" }, "Loading subscriptions…")
  }
  function clearCredentialFields() {
    feedUrl.text = ""; feedUsername.text = ""; feedPassword.text = ""
  }
  function submitSubscription() {
    var checked = Model.validateSubscriptionInput({ name: feedName.text, url: feedUrl.text, username: feedUsername.text, password: feedPassword.text, color: feedColor.text })
    clearCredentialFields()
    if (!checked.valid) { subscriptionError = checked.error; return }
    startSubscription(checked.value, "Adding subscription…")
  }
  function removeSubscription(id) { startSubscription({ action: "remove", id: id }, "Removing subscription…") }
  function refreshSubscriptions() { startSubscription({ action: "refresh" }, "Refreshing subscriptions…") }
  function startSubscription(request, message) {
    if (subscriptionBusy || helperPath === "") return
    subscriptionSequence++
    subscriptionRequestId = "subscription-qml-" + subscriptionSequence
    request.requestId = subscriptionRequestId
    subscriptionAction = request.action
    pendingSubscriptionPayload = JSON.stringify(request)
    request.url = ""; request.username = ""; request.password = ""
    subscriptionState = "running"; subscriptionMessage = message; subscriptionError = ""
    subscriptionProtocolState = { finalSeen: false, progressCount: 0 }
    subscriptionResponseCharacters = 0; subscriptionResponseLines = 0
    subscriptionTimedOut = false; subscriptionCancelled = false; subscriptionCommitStarted = false
    subscriptionProcess.command = [helperPath, "subscriptions"]
    subscriptionProcess.running = true
  }
  function acceptSubscriptionLine(data) {
    var line = String(data || "")
    subscriptionResponseCharacters += line.length + 1; subscriptionResponseLines++
    if (line.length === 0 || line.length > 16384 || subscriptionResponseCharacters > 65536 || subscriptionResponseLines > 64) {
      failSubscriptionProtocol("Subscription helper returned too much data"); return
    }
    var parsed = Model.parseSubscriptionProtocolLine(line, subscriptionRequestId, subscriptionProtocolState)
    if (!parsed.valid) { failSubscriptionProtocol(parsed.error); return }
    subscriptionProtocolState = parsed.state
    if (parsed.kind === "progress") {
      if (parsed.response.stage === "committing") subscriptionCommitStarted = true
      subscriptionMessage = String(parsed.response.stage || "Working…").replace(/^./, function(c) { return c.toUpperCase() }) + "…"
    }
    else {
      var response = parsed.response
      if (response.ok !== true) subscriptionError = String(response.error && response.error.message || "Subscription operation failed").substr(0, 500)
      else {
        subscriptionCancelled = false
        subscriptionState = "success"; subscriptionMessage = subscriptionAction === "refresh" ? "Subscriptions refreshed" : "Subscription updated"
        var warnings = response.cleanupWarnings || (response.refresh && response.refresh.cleanupWarnings)
        if (Array.isArray(warnings) && warnings.length > 0) subscriptionMessage += ". " + String(warnings[0]).substr(0, 300)
        if (Array.isArray(response.subscriptions)) subscriptions = Model.normalizeSubscriptionStatus(response.subscriptions, null)
        if (response.refresh) subscriptions = Model.normalizeSubscriptionStatus(subscriptions, response.refresh)
      }
    }
  }
  function failSubscriptionProtocol(message) {
    clearCredentialFields()
    subscriptionError = message || "Subscription helper returned invalid data"
    if (subscriptionProcess.running) subscriptionProcess.signal(15)
    subscriptionHardKill.restart()
  }
  function cancelSubscription() {
    clearCredentialFields()
    if (!subscriptionBusy || !subscriptionProcess.running || subscriptionCommitStarted) return
    subscriptionCancelled = true; subscriptionState = "cancelling"; subscriptionMessage = "Cancelling subscription operation…"
    subscriptionProcess.signal(15); subscriptionHardKill.restart()
  }
  function finishSubscription(exitCode) {
    subscriptionWatchdog.stop(); subscriptionHardKill.stop(); pendingSubscriptionPayload = ""; clearCredentialFields()
    if (subscriptionState === "success") {}
    else if (subscriptionCancelled) { subscriptionState = "cancelled"; subscriptionMessage = "Subscription operation cancelled" }
    else if (subscriptionTimedOut) { subscriptionState = "error"; subscriptionError = "Subscription operation timed out" }
    else if (!subscriptionProtocolState.finalSeen) { subscriptionState = "error"; if (subscriptionError === "") subscriptionError = exitCode === 0 ? "Subscription helper ended without a result" : "Subscription helper failed" }
    else if (subscriptionError !== "") subscriptionState = "error"
    if (subscriptionState === "success") {
      addFormOpen = false; feedName.text = ""; feedUsername.text = ""; feedColor.text = ""
      refreshData(true)
      if (subscriptionAction !== "list") Qt.callLater(function() { startSubscription({ action: "list" }, "Updating subscriptions…") })
    }
  }
  Component.onDestruction: {
    if (requestProcess.running) requestProcess.signal(15)
    if (subscriptionProcess.running) subscriptionProcess.signal(15)
  }

  Process {
    id: requestProcess
    property string responseText: ""
    property string errorText: ""
    property bool timedOut: false
    stdinEnabled: true
    onStarted: { requestTimeout.restart(); write(JSON.stringify(root.activeRequest) + "\n") }
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: requestProcess.responseText = String(text || "") }
    stderr: StdioCollector { waitForEnd: true; onStreamFinished: requestProcess.errorText = String(text || "") }
    onExited: function(exitCode) { Qt.callLater(function() { root.finishRequest(exitCode) }) }
  }
  Timer { id: requestTimeout; interval: 45000; onTriggered: { if (requestProcess.running) { requestProcess.timedOut = true; requestProcess.signal(15); requestHardKill.restart() } } }
  Timer { id: requestHardKill; interval: 1500; onTriggered: if (requestProcess.running) requestProcess.signal(9) }

  Process {
    id: subscriptionProcess
    stdinEnabled: true
    onStarted: {
      subscriptionWatchdog.restart()
      write(root.pendingSubscriptionPayload + "\n")
      root.pendingSubscriptionPayload = ""
      root.clearCredentialFields()
    }
    stdout: SplitParser { splitMarker: "\n"; onRead: function(data) { root.acceptSubscriptionLine(data) } }
    stderr: SplitParser { splitMarker: "\n"; onRead: function(data) {} }
    onExited: function(exitCode) { Qt.callLater(function() { root.finishSubscription(exitCode) }) }
  }
  Timer {
    id: subscriptionWatchdog; interval: 660000
    onTriggered: { if (subscriptionProcess.running) { root.subscriptionTimedOut = true; root.subscriptionState = "cancelling"; subscriptionProcess.signal(15); subscriptionHardKill.restart() } }
  }
  Timer { id: subscriptionHardKill; interval: 30000; onTriggered: if (subscriptionProcess.running) subscriptionProcess.signal(9) }

  SystemClock {
    id: clock
    precision: SystemClock.Minutes
    onDateChanged: if (Model.keyForDate(clock.date) !== root.todayKey) root.today = clock.date
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
      blocked: root.settingsOpen
      onMoveRequested: function(dx, dy) { root.moveSelection(dx + dy * 7) }
      onActivateRequested: root.selectedKey = root.todayKey
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "[") root.moveMonth(-1)
        else if (text === "]") root.moveMonth(1)
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
            width: parent.width; height: monthNavigation.implicitHeight
            Row {
              id: monthNavigation; anchors.horizontalCenter: parent.horizontalCenter; spacing: Style.space(12)
              Button { iconText: "󰅁"; tooltipText: "Previous month"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; onClicked: root.moveMonth(-1) }
              Text {
                width: Style.space(230); anchors.verticalCenter: parent.verticalCenter; horizontalAlignment: Text.AlignHCenter
                text: Qt.formatDate(root.viewDate, "MMMM yyyy").toUpperCase(); color: root.contentForeground
                font.family: root.contentFontFamily; font.pixelSize: Style.font.title; font.bold: true; font.letterSpacing: 1
              }
              Button { iconText: "󰅂"; tooltipText: "Next month"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; onClicked: root.moveMonth(1) }
            }
            Button {
              anchors.right: parent.right; anchors.rightMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
              iconText: "󰒓"; tooltipText: "ICS subscriptions"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; bordered: false
              onClicked: if (root.settingsOpen) { root.settingsOpen = false; root.clearCredentialFields() } else root.openSettings()
            }
          }

          Column {
            id: monthGrid; visible: !root.settingsOpen; anchors.horizontalCenter: parent.horizontalCenter; spacing: root.cellSpacing
            Row {
              spacing: root.cellSpacing
              Repeater { model: root.weekdays; Text { required property var modelData; width: root.cellWidth; height: Style.space(18); horizontalAlignment: Text.AlignHCenter; text: root.weekdayLabel(modelData); color: Qt.darker(root.contentForeground, 1.5); font.family: root.contentFontFamily; font.pixelSize: Style.font.caption; font.bold: true } }
            }
            Repeater {
              model: root.weeks
              Row {
                required property var modelData; spacing: root.cellSpacing
                Repeater {
                  model: modelData.days
                  Rectangle {
                    id: dayCell
                    required property var modelData
                    readonly property var dayEvents: root.eventsByDate[modelData.key] || []
                    width: root.cellWidth; height: root.cellHeight; radius: Style.cornerRadius
                    color: modelData.key === root.selectedKey ? Style.selectedFillFor(root.contentForeground, Color.accent) : "transparent"
                    border.width: modelData.today || modelData.key === root.selectedKey ? Style.spacing.hairline : 0
                    border.color: Style.normalBorderFor(root.contentForeground, Color.accent)
                    Text { anchors.horizontalCenter: parent.horizontalCenter; y: Style.space(3); text: dayCell.modelData.day; color: dayCell.modelData.inMonth ? root.contentForeground : Qt.darker(root.contentForeground, 2.1); font.family: root.contentFontFamily; font.pixelSize: Style.font.body; font.bold: dayCell.modelData.today || dayCell.modelData.key === root.selectedKey }
                    Row {
                      anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom; anchors.bottomMargin: Style.space(4); spacing: Style.space(2)
                      Repeater { model: Math.min(3, dayCell.dayEvents.length); Rectangle { required property int index; width: Style.space(4); height: width; radius: width / 2; color: dayCell.dayEvents[index].color } }
                    }
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.selectDay(dayCell.modelData) }
                  }
                }
              }
            }
          }

          Text {
            visible: !root.settingsOpen; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter
            text: "UPCOMING FROM " + root.selectedKey; color: Qt.darker(root.contentForeground, 1.35)
            font.family: root.contentFontFamily; font.pixelSize: Style.font.caption; font.bold: true; font.letterSpacing: 1
          }
          Text { visible: !root.settingsOpen && root.listLoading; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; text: "Loading events…"; color: Qt.darker(root.contentForeground, 1.4); font.family: root.contentFontFamily }
          Text { visible: !root.settingsOpen && !root.listLoading && root.listError !== ""; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; text: root.listError; wrapMode: Text.Wrap; color: Color.urgent; font.family: root.contentFontFamily }
          Text { visible: !root.settingsOpen && !root.listLoading && root.listError === "" && root.agendaEvents.length === 0; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; text: "No upcoming events in this view"; color: Qt.darker(root.contentForeground, 1.5); font.family: root.contentFontFamily }
          Column {
            visible: !root.settingsOpen && !root.listLoading && root.listError === ""; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; spacing: Style.space(5)
            Repeater {
              model: root.agendaEvents
              Rectangle {
                required property var modelData; width: parent.width; height: agendaRow.implicitHeight + Style.space(10); radius: Style.cornerRadius
                color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.055)
                Rectangle { width: Style.space(4); height: parent.height; radius: parent.radius; color: modelData.color }
                Row {
                  id: agendaRow; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.leftMargin: Style.space(12); anchors.rightMargin: Style.space(8); spacing: Style.space(8)
                  Text { width: Style.space(92); text: modelData.startKey + "\n" + root.eventTime(modelData); color: Qt.darker(root.contentForeground, 1.35); font.family: root.contentFontFamily; font.pixelSize: Style.font.caption }
                  Text { width: parent.width - Style.space(100); text: modelData.title + "\n" + modelData.calendarName; elide: Text.ElideRight; color: root.contentForeground; font.family: root.contentFontFamily; font.pixelSize: Style.font.body }
                }
              }
            }
          }

          Column {
            visible: root.settingsOpen; width: Style.space(410); anchors.horizontalCenter: parent.horizontalCenter; spacing: Style.space(7)
            Text { text: "ICS SUBSCRIPTIONS"; color: root.contentForeground; font.family: root.contentFontFamily; font.pixelSize: Style.font.body; font.bold: true; font.letterSpacing: 1 }
            Text { width: parent.width; text: "Private feed URLs and optional credentials are stored in Secret Service. URLs are never shown after saving. Maximum 16 subscriptions."; wrapMode: Text.Wrap; color: Qt.darker(root.contentForeground, 1.3); font.family: root.contentFontFamily; font.pixelSize: Style.font.bodySmall }
            Text { width: parent.width; text: "Google Calendar: Settings → Integrate calendar → Secret address in iCal format."; wrapMode: Text.Wrap; color: root.contentForeground; font.family: root.contentFontFamily; font.pixelSize: Style.font.bodySmall }
            Repeater {
              model: root.subscriptions
              Rectangle {
                required property var modelData; width: parent.width; height: Style.space(42); radius: Style.cornerRadius
                color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.055)
                Rectangle { width: Style.space(4); height: parent.height; radius: parent.radius; color: modelData.color || "#7aa2f7" }
                Text { anchors.left: parent.left; anchors.leftMargin: Style.space(12); anchors.verticalCenter: parent.verticalCenter; width: parent.width - removeButton.width - Style.space(24); text: modelData.name + "  ·  " + modelData.statusText; elide: Text.ElideRight; color: modelData.status === "error" ? Color.urgent : root.contentForeground; font.family: root.contentFontFamily; font.pixelSize: Style.font.bodySmall }
                Button { id: removeButton; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: "Remove"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; enabled: !root.subscriptionBusy; onClicked: root.removeSubscription(modelData.id) }
              }
            }
            Row {
              spacing: Style.space(6)
              Button { text: root.addFormOpen ? "Hide add form" : "Add subscription"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; enabled: !root.subscriptionBusy && root.subscriptions.length < 16; onClicked: { root.addFormOpen = !root.addFormOpen; if (!root.addFormOpen) root.clearCredentialFields() } }
              Button { text: "Refresh all"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; bordered: true; enabled: !root.subscriptionBusy && root.subscriptions.length > 0; onClicked: root.refreshSubscriptions() }
              Button { text: "Close"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; onClicked: { root.settingsOpen = false; root.clearCredentialFields() } }
            }
            Column {
              visible: root.addFormOpen; width: parent.width; spacing: Style.space(6)
              TextField { id: feedName; width: parent.width; placeholderText: "Display name"; foreground: root.contentForeground; font.family: root.contentFontFamily; enabled: !root.subscriptionBusy }
              TextField { id: feedUrl; width: parent.width; placeholderText: "Private HTTPS ICS URL"; foreground: root.contentForeground; font.family: root.contentFontFamily; echoMode: TextInput.Password; inputMethodHints: Qt.ImhSensitiveData | Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText; enabled: !root.subscriptionBusy }
              TextField { id: feedUsername; width: parent.width; placeholderText: "Username (optional, requires password)"; foreground: root.contentForeground; font.family: root.contentFontFamily; inputMethodHints: Qt.ImhNoPredictiveText; enabled: !root.subscriptionBusy }
              TextField { id: feedPassword; width: parent.width; placeholderText: "Password (optional, requires username)"; foreground: root.contentForeground; font.family: root.contentFontFamily; echoMode: TextInput.Password; inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText; enabled: !root.subscriptionBusy }
              TextField { id: feedColor; width: parent.width; placeholderText: "Color (optional #RRGGBB)"; foreground: root.contentForeground; font.family: root.contentFontFamily; enabled: !root.subscriptionBusy }
              Button { anchors.right: parent.right; text: root.subscriptionBusy ? "Adding…" : "Add"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; bordered: true; enabled: !root.subscriptionBusy; onClicked: root.submitSubscription() }
            }
          }

          Column {
            visible: root.subscriptionBusy || root.subscriptionMessage !== "" || root.subscriptionError !== ""
            width: root.settingsOpen ? Style.space(410) : monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; spacing: Style.space(4)
            Text { width: parent.width; visible: root.subscriptionMessage !== ""; text: root.subscriptionMessage; wrapMode: Text.Wrap; color: root.subscriptionState === "success" ? Color.accent : root.contentForeground; font.family: root.contentFontFamily; font.pixelSize: Style.font.bodySmall }
            Text { width: parent.width; visible: root.subscriptionError !== ""; text: root.subscriptionError; wrapMode: Text.Wrap; color: Color.urgent; font.family: root.contentFontFamily; font.pixelSize: Style.font.bodySmall }
            Button { visible: root.subscriptionBusy && !root.subscriptionCommitStarted; text: "Cancel operation"; foreground: root.contentForeground; fontFamily: root.contentFontFamily; onClicked: root.cancelSubscription() }
          }
          Text { visible: !root.settingsOpen && root.backendStatus !== ""; width: monthGrid.width; anchors.horizontalCenter: parent.horizontalCenter; text: root.backendStatus; wrapMode: Text.Wrap; color: Color.urgent; font.family: root.contentFontFamily; font.pixelSize: Style.font.caption }
        }
      }
    }
  }
}
