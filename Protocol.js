.pragma library

// The QML side of the NDJSON connection to backend/daemon.py, as a pure reducer.
// No QML globals: OmameterService keeps one `protocol` state, swaps in the state
// each call returns and applies the returned events to its properties.
//
//   encode(state, cmd, fields)     -> { state, line, id }   id assigned, command pending
//   ingest(state, rawLine)         -> { state, events }     one daemon stdout line
//   settle(state, id, ok, error)   -> { state, events }     settle locally (e.g. never sent)
//   daemonDown(state, message)     -> { state, events }     process exited: fail every pending
//   outcome(state, id)             -> { status: "pending" | "ok" | "failed" | "unknown", error }
//
// Events:
//   { type: "state", dashboard, accounts, hubs, settings, layout, proxy, canUndo,
//     nextRefreshAt, refreshing, authSession, providerCatalog }   (missing objects are null)
//   { type: "authSession", session }
//   { type: "localCredentials", items }
//   { type: "toast", message, error }
//   { type: "result", id, cmd, ok, error }                         a command settled
//
// Every command carries an `id`; the daemon answers each with exactly one
// { type: "result", id, ok, error? }. A result for an id that is not pending is ignored.

var MAX_LINE_CHARS = 2 * 1024 * 1024
var MAX_SETTLED = 64

// `tag` keeps ids unique across QML reloads (the service passes a timestamp).
function initialState(tag) {
  return { tag: String(tag === undefined || tag === null ? "" : tag), seq: 0, pending: {}, settled: {}, settledOrder: [] }
}

function copyState(state) {
  var pending = {}
  for (var p in state.pending) pending[p] = state.pending[p]
  var settled = {}
  for (var s in state.settled) settled[s] = state.settled[s]
  return { tag: state.tag, seq: state.seq, pending: pending, settled: settled, settledOrder: state.settledOrder.slice() }
}

function encode(state, cmd, fields) {
  var next = copyState(state)
  next.seq += 1
  var id = (next.tag !== "" ? next.tag + "-" : "") + next.seq
  var payload = { cmd: String(cmd || "") }
  if (fields && typeof fields === "object") {
    for (var k in fields) if (k !== "cmd" && k !== "id") payload[k] = fields[k]
  }
  payload.id = id
  next.pending[id] = { cmd: payload.cmd }
  return { state: next, line: JSON.stringify(payload), id: id }
}

// Move a pending command to settled; null when the id is not pending.
function settleInto(next, id, ok, error) {
  var entry = next.pending[id]
  if (!entry) return null
  delete next.pending[id]
  var result = { type: "result", id: id, cmd: entry.cmd, ok: !!ok, error: ok ? "" : String(error || "") }
  next.settled[id] = { ok: result.ok, error: result.error }
  next.settledOrder.push(id)
  while (next.settledOrder.length > MAX_SETTLED) delete next.settled[next.settledOrder.shift()]
  return result
}

function settle(state, id, ok, error) {
  var next = copyState(state)
  var result = settleInto(next, String(id), ok, error)
  if (!result) return { state: state, events: [] }
  return { state: next, events: [result] }
}

function arrayOr(value) {
  return Array.isArray(value) ? value : []
}

function objectOr(value) {
  return value && typeof value === "object" ? value : null
}

function ingest(state, rawLine) {
  var none = { state: state, events: [] }
  var raw = String(rawLine === undefined || rawLine === null ? "" : rawLine)
  if (raw.length > MAX_LINE_CHARS) return none
  raw = raw.trim()
  if (!raw) return none
  var msg
  try {
    msg = JSON.parse(raw)
  } catch (e) {
    return none
  }
  if (!msg || typeof msg !== "object" || Array.isArray(msg)) return none

  var type = String(msg.type || "")
  if (type === "state") {
    return { state: state, events: [{
      type: "state",
      dashboard: objectOr(msg.dashboard),
      accounts: arrayOr(msg.accounts),
      hubs: arrayOr(msg.hubs),
      settings: objectOr(msg.settings),
      layout: objectOr(msg.layout),
      proxy: objectOr(msg.proxy),
      canUndo: !!msg.canUndo,
      nextRefreshAt: String(msg.nextRefreshAt || ""),
      refreshing: !!msg.refreshing,
      authSession: objectOr(msg.authSession),
      providerCatalog: Array.isArray(msg.providerCatalog) && msg.providerCatalog.length > 0 ? msg.providerCatalog : null
    }] }
  }
  if (type === "auth_update") return { state: state, events: [{ type: "authSession", session: objectOr(msg.session) }] }
  if (type === "local_credentials") return { state: state, events: [{ type: "localCredentials", items: arrayOr(msg.items) }] }
  if (type === "toast") return { state: state, events: [{ type: "toast", message: String(msg.message || ""), error: !!msg.error }] }
  if (type === "result") return settle(state, String(msg.id === undefined || msg.id === null ? "" : msg.id), msg.ok === true, msg.error)
  return none
}

function daemonDown(state, message) {
  var ids = Object.keys(state.pending)
  if (ids.length === 0) return { state: state, events: [] }
  var next = copyState(state)
  var events = []
  var text = String(message || "The Omameter daemon stopped before answering.")
  for (var i = 0; i < ids.length; i++) {
    var result = settleInto(next, ids[i], false, text)
    if (result) events.push(result)
  }
  return { state: next, events: events }
}

function outcome(state, id) {
  var key = String(id === undefined || id === null ? "" : id)
  if (state.pending[key]) return { status: "pending", error: "" }
  var done = state.settled[key]
  if (done) return { status: done.ok ? "ok" : "failed", error: done.error }
  return { status: "unknown", error: "" }
}
