.pragma library

// Popup navigation and account-form state machine for Omameter.
// Pure: no QML globals, no service calls. The widget keeps one `nav` state,
// passes every user action or auth-session change through reduce(), swaps in
// the returned state and runs the returned effects:
//   { cmd: "<serviceMethod>", ...args }   -> service call (see OmameterWidget.runEffect)
//   { ui: "resetScroll" | "open" | "close" }
//
// Rules that live only here:
//   - an auth session that is active or in error shows the auth_flow subpage, also
//     when the popup reopens after the browser stole focus (AGENTS.md QML rule 3);
//   - leaving auth_flow while the session is active or in error sends exactly one cancelAuth;
//   - resetScroll is emitted whenever the shown page (view, dashboard tab, accounts subpage)
//     changes, and on every popup open;
//   - secret drafts are cleared whenever their form stops being (or starts being) the shown page;
//   - canSubmit() is the one "can send" rule per form, for the button and for Enter;
//   - a form that validates live (API key, hub, hub key, manual code) waits for its
//     command: `awaiting` holds { form, id } until commandSettled; it closes (or clears its
//     secret) only on success, and on failure stays open with its drafts and `formError`.
//     The id is born when the widget runs the effect, which reports it with commandSent.
//     Leaving the form's page forgets the wait, so a late commandSettled changes nothing.

// Accounts subpages: list | add_picker | auth_flow | api_key_form | hub_form | account_detail | hub_detail

// Secret draft -> the only subpage allowed to keep it.
var SECRET_DRAFT_OWNERS = {
  apiKey: "api_key_form",
  hubKey: "hub_form",
  replaceHubKey: "hub_detail",
  manualCode: "auth_flow"
}

// Form that waits for its command -> the subpage it lives on.
var AWAITING_FORM_PAGES = {
  apiKey: "api_key_form",
  hub: "hub_form",
  replaceHubKey: "hub_detail",
  manualCode: "auth_flow"
}

var DRAFT_FIELDS = ["apiKey", "org", "domain", "manualCode", "hubAddress", "hubKey", "rename", "replaceHubKey"]

function defaultTabFrom(settings) {
  return (settings && settings.defaultTab === "all") ? "all" : "priority"
}

function emptyDrafts() {
  var d = {}
  for (var i = 0; i < DRAFT_FIELDS.length; i++) d[DRAFT_FIELDS[i]] = ""
  return d
}

function initialState(settings) {
  return {
    viewMode: "dashboard",
    dashboardTab: defaultTabFrom(settings),
    subPage: "list",
    selectedAccountId: "",
    selectedHubId: "",
    expandedPickerKind: "",
    apiKeyKind: "",
    apiKeyMethod: "api_key",
    apiKeyExistingAccountId: "",
    drafts: emptyDrafts(),
    confirmRemoveAccount: false,
    confirmRemoveHub: false,
    hasScannedLocal: false,
    awaiting: null,      // { form, id } while a submitted form waits for its command ("" id = not sent yet)
    formError: null      // { form, message } after the command failed
  }
}

function sessionShowsFlow(session) {
  return !!(session && (session.active || session.status === "error"))
}

function sessionEnded(session) {
  return !session || session.status === "completed" || session.status === "cancelled"
}

function isOrgTokenForm(state) {
  return state.apiKeyMethod === "org_token" || state.apiKeyKind === "copilot"
}

function trimmed(value) {
  return String(value === undefined || value === null ? "" : value).trim()
}

// The single "can send" rule of each form: "manualCode" | "apiKey" | "hub" | "rename" | "replaceHubKey".
function isAwaiting(state, form) {
  return !!(state.awaiting && state.awaiting.form === form)
}

// The error the daemon gave for this form's last submit, or "".
function formError(state, form) {
  return state.formError && state.formError.form === form ? String(state.formError.message || "") : ""
}

function canSubmit(state, form) {
  if (isAwaiting(state, form)) return false
  var d = state.drafts
  if (form === "manualCode") return trimmed(d.manualCode) !== ""
  if (form === "apiKey") return trimmed(d.apiKey) !== "" && (!isOrgTokenForm(state) || trimmed(d.org) !== "")
  if (form === "hub") return trimmed(d.hubAddress) !== "" && trimmed(d.hubKey) !== ""
  if (form === "rename") return state.selectedAccountId !== "" && trimmed(d.rename) !== ""
  if (form === "replaceHubKey") return state.selectedHubId !== "" && trimmed(d.replaceHubKey) !== ""
  return false
}

function shownSubPage(state) {
  return state.viewMode === "accounts" ? state.subPage : ""
}

function shownPage(state) {
  if (state.viewMode === "accounts") return "accounts/" + state.subPage
  if (state.viewMode === "dashboard") return "dashboard/" + state.dashboardTab
  return state.viewMode
}

function copyState(state) {
  var next = {}
  for (var k in state) next[k] = state[k]
  var drafts = {}
  for (var j in state.drafts) drafts[j] = state.drafts[j]
  next.drafts = drafts
  return next
}

function findById(list, id) {
  var arr = Array.isArray(list) ? list : []
  for (var i = 0; i < arr.length; i++) {
    if (arr[i] && String(arr[i].id) === String(id)) return arr[i]
  }
  return null
}

// ---- subpage openers (mutate the working copy `s`) ----

function toList(s) {
  s.subPage = "list"
  s.expandedPickerKind = ""
  s.confirmRemoveAccount = false
  s.confirmRemoveHub = false
}

function toAddPicker(s) {
  s.subPage = "add_picker"
  s.expandedPickerKind = ""
  s.drafts.domain = ""
  s.hasScannedLocal = false
}

function toAccountDetail(s, accountId, ctx) {
  s.selectedAccountId = String(accountId || "")
  var acc = findById(ctx.accounts, s.selectedAccountId)
  s.drafts.rename = acc ? String(acc.label || acc.accountName || "") : ""
  s.confirmRemoveAccount = false
  s.subPage = "account_detail"
}

function toHubDetail(s, hubId) {
  s.selectedHubId = String(hubId || "")
  s.drafts.replaceHubKey = ""
  s.confirmRemoveHub = false
  s.subPage = "hub_detail"
}

function toApiKeyForm(s, kind, method, existingAccountId, ctx) {
  s.apiKeyKind = String(kind || "")
  s.apiKeyMethod = String(method || "api_key")
  s.apiKeyExistingAccountId = String(existingAccountId || "")
  s.drafts.apiKey = ""
  s.drafts.org = ""
  if (s.apiKeyExistingAccountId !== "") {
    var acc = findById(ctx.accounts, s.apiKeyExistingAccountId)
    if (acc && acc.organization) s.drafts.org = String(acc.organization)
  }
  s.subPage = "api_key_form"
}

function toHubForm(s) {
  s.drafts.hubAddress = ""
  s.drafts.hubKey = ""
  s.subPage = "hub_form"
}

function toAuthFlow(s, effects, kind, method, accountId, domain) {
  s.drafts.manualCode = ""
  s.subPage = "auth_flow"
  effects.push({
    cmd: "startAuth",
    kind: String(kind || ""),
    method: String(method || "oauth"),
    accountId: accountId ? String(accountId) : null,
    domain: domain ? String(domain) : null
  })
}

// Subpage "back" (the in-view back/cancel buttons and the widget's back key inside accounts).
function backFromSubPage(s) {
  if (s.subPage === "api_key_form" && s.apiKeyExistingAccountId !== "") {
    s.subPage = "account_detail"
    return
  }
  toList(s)
}

function isApiKeyMethod(method) {
  return method === "api_key" || method === "org_token"
}

// Submit of a form that waits: send, remember the wait, drop the previous error.
function awaitCommand(s, effects, form, effect) {
  effect.awaits = form
  effects.push(effect)
  s.awaiting = { form: form, id: "" }
  s.formError = null
}

// The command of a waiting form succeeded: same navigation and clearing as a plain submit.
function formSucceeded(s, form) {
  if (form === "apiKey") {
    s.drafts.apiKey = ""
    backFromSubPage(s)
  } else if (form === "hub") {
    s.drafts.hubKey = ""
    toList(s)
  } else if (form === "replaceHubKey") {
    s.drafts.replaceHubKey = ""
  } else if (form === "manualCode") {
    s.drafts.manualCode = ""
  }
}

// ---- the reducer ----

function reduce(state, action, ctx) {
  ctx = ctx || {}
  action = action || {}
  var s = copyState(state)
  var effects = []
  var session = ctx.authSession || null
  var reopened = false

  switch (action.type) {
  case "popupOpened":
    session = action.session || null
    reopened = true
    if (sessionShowsFlow(session)) {
      s.viewMode = "accounts"
      s.subPage = "auth_flow"
    } else {
      s.viewMode = "dashboard"
      s.dashboardTab = defaultTabFrom(ctx.settings)
    }
    break

  case "authSessionChanged":
    session = action.session || null
    if (sessionShowsFlow(session)) {
      s.subPage = "auth_flow"
    } else if (s.subPage === "auth_flow" && sessionEnded(session)) {
      toList(s)
    }
    break

  case "showDashboard":
    s.viewMode = "dashboard"
    break

  case "setDashboardTab":
    s.viewMode = "dashboard"
    s.dashboardTab = action.tab === "all" ? "all" : "priority"
    break

  case "openAccounts":
    s.viewMode = "accounts"
    if (action.accountId) toAccountDetail(s, action.accountId, ctx)
    else toList(s)
    effects.push({ ui: "open" })
    break

  case "openAddAccount":
    s.viewMode = "accounts"
    toAddPicker(s)
    effects.push({ ui: "open" })
    break

  case "toggleAccounts":
    if (s.viewMode === "accounts") {
      s.viewMode = "dashboard"
    } else {
      s.viewMode = "accounts"
      toList(s)
      effects.push({ ui: "open" })
    }
    break

  case "openSettings":
    if (ctx.opened && s.viewMode === "settings") {
      effects.push({ ui: "close" })
    } else {
      s.viewMode = "settings"
      effects.push({ ui: "open" })
    }
    break

  case "toggleSettings":
    s.viewMode = s.viewMode === "settings" ? "dashboard" : "settings"
    break

  case "back":
    if (s.viewMode === "accounts") {
      if (s.subPage !== "list") backFromSubPage(s)
      else s.viewMode = "dashboard"
    } else if (s.viewMode === "settings") {
      s.viewMode = "dashboard"
    } else {
      effects.push({ ui: "close" })
    }
    break

  case "openList":
    toList(s)
    break

  case "openAddPicker":
    toAddPicker(s)
    break

  case "openAccountDetail":
    toAccountDetail(s, action.accountId, ctx)
    break

  case "openHubDetail":
    toHubDetail(s, action.hubId)
    break

  case "openApiKeyForm":
    toApiKeyForm(s, action.kind, action.method, action.accountId, ctx)
    break

  case "openHubForm":
    toHubForm(s)
    break

  case "togglePickerKind":
    s.expandedPickerKind = s.expandedPickerKind === action.kind ? "" : String(action.kind || "")
    s.drafts.domain = ""
    break

  // A sign-in method chosen in the add picker: key methods open the key form,
  // the others start interactive auth (Copilot carries the GitHub domain draft).
  case "pickMethod":
    if (isApiKeyMethod(action.method)) {
      toApiKeyForm(s, action.kind, action.method, "", ctx)
    } else {
      var domain = (action.kind === "copilot" && trimmed(s.drafts.domain) !== "") ? trimmed(s.drafts.domain) : null
      toAuthFlow(s, effects, action.kind, action.method, null, domain)
    }
    break

  case "beginAuth":
    toAuthFlow(s, effects, action.kind, action.method, action.accountId, action.domain)
    break

  case "cancelAuth":
    toList(s)
    break

  case "scanLocal":
    s.hasScannedLocal = true
    effects.push({ cmd: "detectLocalCredentials" })
    break

  case "setDraft":
    if (DRAFT_FIELDS.indexOf(action.field) >= 0) s.drafts[action.field] = String(action.value === undefined || action.value === null ? "" : action.value)
    break

  case "submitManualCode":
    if (canSubmit(s, "manualCode")) {
      awaitCommand(s, effects, "manualCode", { cmd: "submitAuthCode", code: trimmed(s.drafts.manualCode) })
    }
    break

  case "submitApiKey":
    if (canSubmit(s, "apiKey")) {
      awaitCommand(s, effects, "apiKey", {
        cmd: "addApiKeyAccount",
        kind: s.apiKeyKind,
        apiKey: trimmed(s.drafts.apiKey),
        organization: isOrgTokenForm(s) ? trimmed(s.drafts.org) : "",
        accountId: s.apiKeyExistingAccountId
      })
    }
    break

  case "submitHub":
    if (canSubmit(s, "hub")) {
      awaitCommand(s, effects, "hub", { cmd: "addHub", address: trimmed(s.drafts.hubAddress), managementKey: trimmed(s.drafts.hubKey) })
    }
    break

  // The widget ran a waiting form's effect; `id` is the command id ("" = could not send).
  case "commandSent":
    if (!s.awaiting || s.awaiting.form !== action.form || s.awaiting.id !== "") return { state: state, effects: [] }
    if (action.id) {
      s.awaiting = { form: s.awaiting.form, id: String(action.id) }
    } else {
      s.formError = { form: s.awaiting.form, message: String(action.error || "") }
      s.awaiting = null
    }
    break

  case "commandSettled":
    if (!s.awaiting || s.awaiting.id === "" || s.awaiting.id !== String(action.id)) return { state: state, effects: [] }
    var settledForm = s.awaiting.form
    s.awaiting = null
    if (action.ok) formSucceeded(s, settledForm)
    else s.formError = { form: settledForm, message: String(action.error || "") }
    break

  case "submitRename":
    if (canSubmit(s, "rename")) {
      effects.push({ cmd: "renameAccount", accountId: s.selectedAccountId, label: trimmed(s.drafts.rename) })
    }
    break

  case "submitReplaceHubKey":
    if (canSubmit(s, "replaceHubKey")) {
      awaitCommand(s, effects, "replaceHubKey", { cmd: "replaceHubKey", hubId: s.selectedHubId, managementKey: trimmed(s.drafts.replaceHubKey) })
    }
    break

  case "askRemoveAccount":
    s.confirmRemoveAccount = !!action.confirm
    break

  case "confirmRemoveAccount":
    if (s.selectedAccountId !== "") effects.push({ cmd: "removeAccount", accountId: s.selectedAccountId })
    toList(s)
    break

  case "askRemoveHub":
    s.confirmRemoveHub = !!action.confirm
    break

  case "confirmRemoveHub":
    if (s.selectedHubId !== "") effects.push({ cmd: "removeHub", hubId: s.selectedHubId })
    toList(s)
    break

  default:
    return { state: state, effects: [] }
  }

  // Leaving auth_flow by any user action cancels a live (or failed) session, once.
  if (action.type !== "authSessionChanged" && action.type !== "popupOpened"
      && state.subPage === "auth_flow" && s.subPage !== "auth_flow" && sessionShowsFlow(session)) {
    effects.unshift({ cmd: "cancelAuth" })
  }

  // Secret drafts survive only while their own form stays the shown page:
  // leaving it (submit, cancel, back, any navigation) or arriving at it starts clean.
  var shownBefore = shownSubPage(state)
  var shownAfter = shownSubPage(s)
  for (var field in SECRET_DRAFT_OWNERS) {
    var owner = SECRET_DRAFT_OWNERS[field]
    if (owner !== shownBefore || owner !== shownAfter) s.drafts[field] = ""
  }
  // A form's wait and error follow the same rule: they belong to the page that shows them.
  if (s.awaiting) {
    var awaitPage = AWAITING_FORM_PAGES[s.awaiting.form]
    if (awaitPage !== shownBefore || awaitPage !== shownAfter) s.awaiting = null
  }
  if (s.formError) {
    var errorPage = AWAITING_FORM_PAGES[s.formError.form]
    if (errorPage !== shownBefore || errorPage !== shownAfter) s.formError = null
  }

  if (reopened || shownPage(state) !== shownPage(s)) effects.push({ ui: "resetScroll" })

  return { state: s, effects: effects }
}
