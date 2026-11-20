import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "../Model.js" as Model
import "../Settings.js" as Settings
import "../Navigation.js" as Navigation

// Full Account & CLIProxyAPI Hub manager:
//   - "list": connected accounts + connected CLIProxyAPI hubs + Add/Connect buttons
//   - "add_picker": all 13 providers + CLIProxyAPI Hub (inline method choice & Copilot GHE domain)
//   - "auth_flow": interactive Browser OAuth (with manual code paste) & Device Code flow
//   - "api_key_form": API Key or Copilot Organization PAT entry / key replacement
//   - "hub_form": connect a CLIProxyAPI hub
//   - "account_detail": rename, re-auth/replace key, metric visibility/pins/reorder, remove
//   - "hub_detail": sync now, replace management key, remove hub
Column {
  id: root

  property var service: null
  property var accounts: []
  property var hubs: []
  property var providerCatalog: []
  property var authSession: null
  property var settings: Settings.defaultSettings()
  property var palette: ({})
  property var strings: null
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.family

  // Navigation + draft state lives in Navigation.js; the widget owns it and runs its effects.
  property var nav: Navigation.initialState(Settings.defaultSettings())
  signal navigationRequested(var action)
  readonly property string subPage: nav.subPage
  readonly property var drafts: nav.drafts

  readonly property bool privacyMode: !!(settings && settings.privacyMode)
  readonly property var catalog: Model.safeProviderCatalog(providerCatalog)
  readonly property var detectedLocalList: (service && Array.isArray(service.detectedCredentials)) ? service.detectedCredentials : []
  readonly property bool detectingLocal: !!(service && service.detectingLocal)

  readonly property bool inputFocused:
    manualCodeInput.fieldFocused
    || apiKeyInput.fieldFocused
    || orgInput.fieldFocused
    || hubAddressInput.fieldFocused
    || hubKeyInput.fieldFocused
    || detailRenameInput.fieldFocused
    || replaceHubKeyInput.fieldFocused
    || (pickerDomainFocused && subPage === "add_picker")

  // Qt focus state of the picker's domain field (not navigation state).
  property bool pickerDomainFocused: false

  function dispatch(action) {
    navigationRequested(action)
  }

  // The daemon's error for a form's last submit (Navigation.formError), safe for display.
  function formErrorText(form) {
    return Model.cleanText(Navigation.formError(nav, form), 260)
  }

  // A waiting form's button says it is checking instead of its action.
  function submitLabel(form, label) {
    return Navigation.isAwaiting(nav, form) ? t("accounts.checking", "Checking…") : label
  }

  function t(key, fb) { return Model.tr(strings, key, fb) }

  function tn(key, count, fb) {
    return strings ? strings.tn(key, count, fb) : (fb || key)
  }

  function providerSpec(kind) {
    for (var i = 0; i < catalog.length; i++) {
      if (catalog[i].id === kind) return catalog[i]
    }
    return { id: kind, displayName: kind, icon: "omameter.svg", authMethods: [], keyPage: "" }
  }

  function currentAccount() {
    var list = accounts || []
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === nav.selectedAccountId) return list[i]
    }
    return null
  }

  function currentHub() {
    var list = hubs || []
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === nav.selectedHubId) return list[i]
    }
    return null
  }

  spacing: Style.space(10)

  // ==================== LIST MODE ====================
  Column {
    visible: root.subPage === "list"
    width: parent.width
    spacing: Style.space(10)

    // Connected Accounts section
    Column {
      width: parent.width
      spacing: Style.space(6)

      Item {
        width: parent.width
        height: Math.max(accHead.implicitHeight, addAccTopBtn.implicitHeight)

        PanelSectionHeader {
          id: accHead
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("accounts.title", "Accounts")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Button {
          id: addAccTopBtn
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("accounts.addNew", "Add New Account")
          iconText: "+"
          bordered: true
          selected: (root.accounts || []).length === 0
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          horizontalPadding: Style.space(8)
          verticalPadding: Style.space(3)
          onClicked: root.dispatch({ type: "openAddPicker" })
        }
      }

      PlainText {
        visible: (root.accounts || []).length === 0
        width: parent.width
        text: root.t("empty.subtitle", "Add your AI coding subscriptions to track limits right from the top bar.")
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.65)
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Repeater {
        model: root.accounts || []
        delegate: ListCard {
          required property var modelData
          width: parent.width
          icon: modelData.icon || "omameter.svg"
          isHub: !!modelData.hubId
          badgeBackground: root.background
          title: Model.accountHeader(modelData.displayName || modelData.kind, modelData.accountName, { privacyMode: root.privacyMode })
          pillText: modelData.plan && String(modelData.plan).trim() !== "" ? String(modelData.plan) : ""
          subtitle: modelData.error
            ? Model.cleanText(modelData.error, 140)
            : (modelData.hubHost ? ("CLIProxyAPI · " + modelData.hubHost) : Model.authMethodLabel(modelData.authMethod, root.strings))
          subtitleVisible: true
          alert: !!modelData.error
          foreground: root.foreground
          accent: root.accent
          alertColor: Model.isTransientErrorKind(modelData.errorKind)
            ? (root.palette.warning || "#e59b3c")
            : (root.palette.critical || root.urgent)
          alertBorderColor: Model.isTransientErrorKind(modelData.errorKind)
            ? (root.palette.warning || "#e59b3c")
            : root.urgent
          fontFamily: root.fontFamily
          onClicked: root.dispatch({ type: "openAccountDetail", accountId: modelData.id })
        }
      }
    }

    PanelSeparator { foreground: root.foreground }

    // CLIProxyAPI Hubs section
    Column {
      width: parent.width
      spacing: Style.space(6)

      Item {
        width: parent.width
        height: Math.max(hubHead.implicitHeight, hubHeadActions.implicitHeight)

        PanelSectionHeader {
          id: hubHead
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("accounts.hubs", "CLIProxyAPI Hubs")
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Row {
          id: hubHeadActions
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(4)

          PanelActionButton {
            visible: (root.hubs || []).length > 0
            iconText: "󰑐"
            tooltipText: Model.autoTextSafe(root.t("card.refresh", "Refresh"), false)
            size: Style.space(22)
            fontSize: Style.font.caption
            foreground: root.foreground
            hoverColor: root.accent
            fontFamily: root.fontFamily
            onClicked: if (root.service) root.service.syncHubs(true)
          }

          Button {
            text: root.t("accounts.addHub", "Connect CLIProxyAPI Hub")
            iconText: "+"
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(8)
            verticalPadding: Style.space(3)
            onClicked: root.dispatch({ type: "openHubForm" })
          }
        }
      }

      Repeater {
        model: root.hubs || []
        delegate: ListCard {
          required property var modelData
          width: parent.width
          icon: "cliproxyapi.svg"
          title: String(modelData.host || modelData.baseUrl || "")
          subtitle: modelData.lastError
            ? Model.cleanText(modelData.lastError, 140)
            : (Number(modelData.unsupportedCount) > 0
              ? root.tn("accounts.unsupportedLogins", Number(modelData.unsupportedCount), "%1 unsupported logins on hub").arg(Number(modelData.unsupportedCount))
              : String(modelData.baseUrl || ""))
          alert: !!modelData.lastError
          hoverBorder: false
          foreground: root.foreground
          accent: root.accent
          alertColor: root.palette.critical || root.urgent
          alertBorderColor: root.urgent
          fontFamily: root.fontFamily
          onClicked: root.dispatch({ type: "openHubDetail", hubId: modelData.id })
        }
      }
    }
  }

  // ==================== ADD PICKER MODE ====================
  Column {
    visible: root.subPage === "add_picker"
    width: parent.width
    spacing: Style.space(6)

    PanelSectionHeader {
      text: root.t("accounts.addNew", "Add New Account")
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    // Opt-in local CLI login detection card
    BorderSurface {
      width: parent.width
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      implicitHeight: localDetectCol.implicitHeight + Style.space(14)

      Column {
        id: localDetectCol
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(8)
        anchors.rightMargin: Style.space(8)
        spacing: Style.space(6)

        Item {
          width: parent.width
          height: Math.max(localHeadCol.implicitHeight, detectLocalBtn.implicitHeight)

          Column {
            id: localHeadCol
            anchors.left: parent.left
            anchors.right: detectLocalBtn.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            PlainText {
              width: parent.width
              text: root.t("accounts.detectLocal", "Detect Local Logins")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              elide: Text.ElideRight
            }

            PlainText {
              width: parent.width
              text: root.t("accounts.detectLocalHint", "Scan local CLI auth files on this machine")
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Button {
            id: detectLocalBtn
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: "󰑐"
            iconSpinning: root.detectingLocal
            iconSize: Style.font.caption + 1
            text: root.detectingLocal
              ? root.t("accounts.detectingLocal", "Scanning…")
              : root.t("accounts.detectLocal", "Detect Local Logins")
            bordered: true
            enabled: !root.detectingLocal
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(7)
            verticalPadding: Style.space(3)
            onClicked: root.dispatch({ type: "scanLocal" })
          }
        }

        PlainText {
          visible: root.nav.hasScannedLocal && !root.detectingLocal && root.detectedLocalList.length === 0
          width: parent.width
          text: root.t("accounts.noLocalFound", "No local CLI logins found.")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Column {
          visible: root.nav.hasScannedLocal && root.detectedLocalList.length > 0
          width: parent.width
          spacing: Style.space(5)

          PanelSeparator { foreground: root.foreground }

          Repeater {
            model: root.detectedLocalList
            delegate: Item {
              required property var modelData
              readonly property var candSpec: root.providerSpec(String(modelData.kind || ""))
              readonly property bool alreadyAdded: !!modelData.alreadyAdded

              width: parent.width
              height: Math.max(candInfoRow.implicitHeight, candActionBox.implicitHeight)

              Row {
                id: candInfoRow
                anchors.left: parent.left
                anchors.right: candActionBox.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(7)

                BrandMark {
                  anchors.verticalCenter: parent.verticalCenter
                  icon: modelData.icon || candSpec.icon || "omameter.svg"
                  iconSize: Style.space(15)
                  tintColor: root.foreground
                  badgeBackground: root.background
                  fontFamily: root.fontFamily
                }

                Column {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.max(40, candInfoRow.width - Style.space(22))
                  spacing: Style.space(1)

                  PlainText {
                    width: parent.width
                    // The detected label is usually an email: privacy hides it like any account name.
                    text: modelData.label
                      ? Model.displayAccountName(modelData.label, { privacyMode: root.privacyMode })
                      : Model.cleanText(candSpec.displayName || modelData.kind || "", 120)
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                    elide: Text.ElideRight
                  }

                  PlainText {
                    visible: text !== ""
                    width: parent.width
                    text: Model.cleanText(modelData.source || "", 140)
                    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }

              Item {
                id: candActionBox
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: alreadyAdded ? addedPill.implicitWidth : importCandBtn.implicitWidth
                implicitHeight: alreadyAdded ? addedPill.implicitHeight : importCandBtn.implicitHeight
                width: implicitWidth
                height: implicitHeight

                Pill {
                  id: addedPill
                  visible: alreadyAdded
                  anchors.centerIn: parent
                  paddingX: Style.space(10)
                  paddingY: Style.space(4)
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.10)
                  text: root.t("accounts.added", "Added")
                  textColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.6)
                  fontFamily: root.fontFamily
                }

                Button {
                  id: importCandBtn
                  visible: !alreadyAdded
                  anchors.centerIn: parent
                  text: root.t("accounts.import", "Import")
                  bordered: true
                  selected: true
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(8)
                  verticalPadding: Style.space(3)
                  onClicked: if (root.service) root.service.importLocalCredential(modelData.id)
                }
              }
            }
          }
        }
      }
    }

    Repeater {
      model: root.catalog
      delegate: BorderSurface {
        id: pickCard
        required property var modelData
        readonly property var methods: modelData.authMethods || []
        readonly property bool isMultiMethod: methods.length > 1 || modelData.id === "copilot"
        readonly property bool expanded: root.nav.expandedPickerKind === modelData.id
        readonly property bool rowHot: pickHover.hovered

        width: parent.width
        radius: Style.cornerRadius
        color: rowHot && !expanded
          ? Style.hoverFillFor(root.foreground, root.accent)
          : Style.normalFillFor(root.foreground, root.accent)
        Behavior on color { ColorAnimation { duration: Style.duration(80) } }
        borderSpec: Border.controlSpec(expanded ? "selected" : (rowHot ? "hover-cursor" : "normal"), root.foreground, root.accent)
        implicitHeight: pickCol.implicitHeight + Style.space(12)

        HoverHandler { id: pickHover }

        Column {
          id: pickCol
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(8)
          anchors.rightMargin: Style.space(8)
          spacing: Style.space(6)

          Item {
            width: parent.width
            height: Math.max(pLeft.implicitHeight, pRight.implicitHeight)

            Row {
              id: pLeft
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(8)

              BrandMark {
                anchors.verticalCenter: parent.verticalCenter
                icon: modelData.icon || "omameter.svg"
                iconSize: Style.space(16)
                tintColor: root.foreground
                fontFamily: root.fontFamily
              }

              PlainText {
                anchors.verticalCenter: parent.verticalCenter
                text: String(modelData.displayName || modelData.id)
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
            }

            PlainText {
              id: pRight
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: pickCard.methods.map(function(m) { return Model.authMethodLabel(m, root.strings) }).join(" / ")
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                if (pickCard.isMultiMethod) {
                  root.dispatch({ type: "togglePickerKind", kind: modelData.id })
                } else {
                  root.dispatch({ type: "pickMethod", kind: modelData.id, method: pickCard.methods[0] || "oauth" })
                }
              }
            }
          }

          // Expanded options for multi-method providers (ChatGPT, Copilot, OpenCode)
          Column {
            visible: pickCard.expanded
            width: parent.width
            spacing: Style.space(6)

            // Optional GitHub Enterprise domain input for Copilot
            FormField {
              visible: modelData.id === "copilot"
              width: parent.width
              spacing: Style.space(3)
              label: root.t("accounts.domainLabel", "GitHub Domain (blank for github.com)")
              labelOpacity: 0.72
              fieldVerticalPadding: Style.space(4)
              placeholderText: root.t("accounts.domainPlaceholder", "github.com or octocorp.ghe.com")
              text: root.drafts.domain
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              onEdited: function(value) { root.dispatch({ type: "setDraft", field: "domain", value: value }) }
              onFieldFocusedChanged: root.pickerDomainFocused = fieldFocused
            }

            Flow {
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: pickCard.methods
                delegate: Button {
                  required property var modelData
                  text: Model.authMethodLabel(modelData, root.strings)
                  bordered: true
                  foreground: root.foreground
                  accent: root.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(8)
                  verticalPadding: Style.space(4)
                  onClicked: root.dispatch({ type: "pickMethod", kind: pickCard.modelData.id, method: modelData })
                }
              }
            }
          }
        }
      }
    }

    PanelSeparator { foreground: root.foreground }

    // CLIProxyAPI Hub option in picker
    ListCard {
      width: parent.width
      icon: "cliproxyapi.svg"
      title: root.t("accounts.addHub", "Connect CLIProxyAPI Hub")
      showChevron: false
      verticalPadding: Style.space(12)
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onClicked: root.dispatch({ type: "openHubForm" })
    }
  }

  // ==================== INTERACTIVE AUTH FLOW (OAuth / Device Code) ====================
  BorderSurface {
    id: authFlowCard
    visible: root.subPage === "auth_flow"
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", isError ? root.urgent : root.foreground, isError ? root.urgent : root.accent)
    implicitHeight: authCol.implicitHeight + Style.space(22)

    readonly property var sess: root.authSession || {}
    readonly property var spec: root.providerSpec(sess.kind || "")
    readonly property bool isError: sess.status === "error"
    readonly property string userCode: sess.userCode ? String(sess.userCode) : ""
    readonly property string verifyUrl: sess.verificationUrl ? String(sess.verificationUrl) : ""
    readonly property bool acceptsManual: !isError && !!(sess.acceptsManualCode || sess.method === "oauth")

    Column {
      id: authCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(12)
      spacing: Style.space(10)

      Row {
        spacing: Style.space(8)

        BrandMark {
          anchors.verticalCenter: parent.verticalCenter
          icon: authFlowCard.spec.icon || "omameter.svg"
          iconSize: Style.space(18)
          tintColor: authFlowCard.isError ? (root.palette.critical || root.urgent) : root.accent
          fontFamily: root.fontFamily
        }

        PlainText {
          anchors.verticalCenter: parent.verticalCenter
          text: String(authFlowCard.spec.displayName || root.t("accounts.signIn", "Sign In"))
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
      }

      // Status / Waiting message
      PlainText {
        width: parent.width
        text: authFlowCard.sess.error
          ? Model.cleanText(authFlowCard.sess.error, 260)
          : (authFlowCard.sess.message
            ? Model.cleanText(authFlowCard.sess.message, 260)
            : root.t("accounts.waitingAuth", "Waiting for sign-in…"))
        color: authFlowCard.sess.error
          ? (root.palette.critical || root.urgent)
          : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.8)
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      // Device code display box
      Column {
        visible: !authFlowCard.isError && authFlowCard.userCode !== ""
        width: parent.width
        spacing: Style.space(6)

        PlainText {
          text: root.t("accounts.deviceCodeTitle", "Enter this code in your browser:")
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        BorderSurface {
          width: parent.width
          radius: Style.cornerRadius
          color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
          borderSpec: Border.controlSpec("selected", root.foreground, root.accent)
          implicitHeight: codeTxt.implicitHeight + Style.space(16)

          PlainText {
            id: codeTxt
            anchors.centerIn: parent
            text: authFlowCard.userCode
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            font.bold: true
            font.letterSpacing: 2
          }
        }

        Row {
          spacing: Style.space(6)

          Button {
            text: root.t("accounts.copyCode", "Copy Code")
            iconText: "󰆏"
            bordered: true
            selected: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: if (root.service) root.service.copyText(authFlowCard.userCode)
          }

          Button {
            visible: authFlowCard.verifyUrl !== ""
            text: root.t("accounts.openBrowser", "Open Browser")
            iconText: "↗"
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: if (root.service) root.service.openUrl(authFlowCard.verifyUrl)
          }
        }
      }

      // Browser OAuth Open Browser button (when no device code)
      Button {
        visible: !authFlowCard.isError && authFlowCard.userCode === "" && authFlowCard.verifyUrl !== ""
        text: root.t("accounts.signInBrowser", "Open Browser to Sign In")
        iconText: "↗"
        bordered: true
        selected: true
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        onClicked: if (root.service) root.service.openUrl(authFlowCard.verifyUrl)
      }

      // Manual authorization code input (for OAuth / Claude / etc.)
      FormField {
        id: manualCodeInput
        visible: authFlowCard.acceptsManual
        width: parent.width
        spacing: Style.space(5)
        label: root.t("accounts.pasteCode", "Paste authorization code (if shown in browser)")
        labelOpacity: 0.72
        wrapLabel: true
        text: root.drafts.manualCode
        buttonText: root.submitLabel("manualCode", root.t("accounts.continue", "Continue"))
        buttonSelected: Navigation.canSubmit(root.nav, "manualCode")
        buttonEnabled: Navigation.canSubmit(root.nav, "manualCode")
        errorText: root.formErrorText("manualCode")
        foreground: root.foreground
        accent: root.accent
        errorColor: root.palette.critical || root.urgent
        fontFamily: root.fontFamily
        onEdited: function(value) { root.dispatch({ type: "setDraft", field: "manualCode", value: value }) }
        onAccepted: root.dispatch({ type: "submitManualCode" })
        onButtonClicked: root.dispatch({ type: "submitManualCode" })
      }

      Row {
        anchors.right: parent.right
        spacing: Style.space(6)

        Button {
          visible: authFlowCard.isError && !!(authFlowCard.sess.kind && authFlowCard.sess.method)
          text: root.t("accounts.signInAgain", "Sign In Again")
          bordered: true
          selected: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({
            type: "beginAuth",
            kind: String(authFlowCard.sess.kind || ""),
            method: String(authFlowCard.sess.method || "oauth"),
            accountId: authFlowCard.sess.accountId || null,
            domain: authFlowCard.sess.domain || null
          })
        }

        Button {
          text: root.t("accounts.cancel", "Cancel")
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({ type: "cancelAuth" })
        }
      }
    }
  }

  // ==================== API KEY / ORG TOKEN FORM ====================
  BorderSurface {
    id: keyFormCard
    visible: root.subPage === "api_key_form"
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
    implicitHeight: keyFormCol.implicitHeight + Style.space(22)

    readonly property var spec: root.providerSpec(root.nav.apiKeyKind)
    readonly property bool isOrgToken: Navigation.isOrgTokenForm(root.nav)

    Column {
      id: keyFormCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(12)
      spacing: Style.space(8)

      Item {
        width: parent.width
        height: Math.max(kfTitle.implicitHeight, kfKeyPage.implicitHeight)

        Row {
          id: kfTitle
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(8)

          BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: keyFormCard.spec.icon || "omameter.svg"
            iconSize: Style.space(16)
            tintColor: root.foreground
            fontFamily: root.fontFamily
          }

          PlainText {
            anchors.verticalCenter: parent.verticalCenter
            text: String(keyFormCard.spec.displayName || root.nav.apiKeyKind)
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
        }

        Button {
          id: kfKeyPage
          visible: !!(keyFormCard.spec.keyPage && keyFormCard.spec.keyPage !== "")
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("accounts.getApiKey", "Open API Keys Page")
          iconText: "↗"
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          horizontalPadding: Style.space(7)
          verticalPadding: Style.space(3)
          onClicked: if (root.service) root.service.openUrl(keyFormCard.spec.keyPage)
        }
      }

      // Organization field for Copilot Org Token
      FormField {
        id: orgInput
        visible: keyFormCard.isOrgToken
        width: parent.width
        label: root.t("accounts.orgLabel", "Organization")
        placeholderText: root.t("accounts.orgPlaceholder", "my-org")
        text: root.drafts.org
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onEdited: function(value) { root.dispatch({ type: "setDraft", field: "org", value: value }) }
      }

      // API Key / PAT input
      FormField {
        id: apiKeyInput
        width: parent.width
        label: keyFormCard.isOrgToken
          ? root.t("accounts.tokenLabel", "Personal Access Token")
          : root.t("accounts.apiKeyLabel", "API Key")
        password: true
        placeholderText: root.t("accounts.apiKeyPlaceholder", "Paste API key…")
        text: root.drafts.apiKey
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onEdited: function(value) { root.dispatch({ type: "setDraft", field: "apiKey", value: value }) }
        onAccepted: root.dispatch({ type: "submitApiKey" })
      }

      Row {
        anchors.right: parent.right
        spacing: Style.space(6)

        Button {
          text: root.t("accounts.cancel", "Cancel")
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({ type: "back" })
        }

        Button {
          text: root.submitLabel("apiKey", root.nav.apiKeyExistingAccountId !== ""
            ? root.t("accounts.save", "Save")
            : root.t("accounts.add", "Add Account"))
          bordered: true
          selected: true
          enabled: Navigation.canSubmit(root.nav, "apiKey")
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({ type: "submitApiKey" })
        }
      }

      PlainText {
        visible: text !== ""
        width: parent.width
        text: root.formErrorText("apiKey")
        color: root.palette.critical || root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }

  // ==================== HUB CONNECT FORM ====================
  BorderSurface {
    visible: root.subPage === "hub_form"
    width: parent.width
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, root.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
    implicitHeight: hubFormCol.implicitHeight + Style.space(22)

    Column {
      id: hubFormCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(12)
      spacing: Style.space(8)

      Row {
        spacing: Style.space(8)

        BrandMark {
          anchors.verticalCenter: parent.verticalCenter
          icon: "cliproxyapi.svg"
          iconSize: Style.space(16)
          tintColor: root.foreground
          fontFamily: root.fontFamily
        }

        PlainText {
          anchors.verticalCenter: parent.verticalCenter
          text: root.t("accounts.addHub", "Connect CLIProxyAPI Hub")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
      }

      FormField {
        id: hubAddressInput
        width: parent.width
        label: root.t("accounts.hubHostLabel", "Hub Address")
        placeholderText: root.t("accounts.hubHostPlaceholder", "hub.example.com:8317")
        text: root.drafts.hubAddress
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onEdited: function(value) { root.dispatch({ type: "setDraft", field: "hubAddress", value: value }) }
      }

      FormField {
        id: hubKeyInput
        width: parent.width
        label: root.t("accounts.hubKeyLabel", "Management Key")
        password: true
        placeholderText: "••••••••"
        text: root.drafts.hubKey
        foreground: root.foreground
        accent: root.accent
        fontFamily: root.fontFamily
        onEdited: function(value) { root.dispatch({ type: "setDraft", field: "hubKey", value: value }) }
        onAccepted: root.dispatch({ type: "submitHub" })
      }

      Row {
        anchors.right: parent.right
        spacing: Style.space(6)

        Button {
          text: root.t("accounts.cancel", "Cancel")
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({ type: "back" })
        }

        Button {
          text: root.submitLabel("hub", root.t("accounts.hubConnect", "Connect Hub"))
          bordered: true
          selected: true
          enabled: Navigation.canSubmit(root.nav, "hub")
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: root.dispatch({ type: "submitHub" })
        }
      }

      PlainText {
        visible: text !== ""
        width: parent.width
        text: root.formErrorText("hub")
        color: root.palette.critical || root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }

  // ==================== ACCOUNT DETAIL MODE ====================
  Column {
    id: detailCol
    visible: root.subPage === "account_detail" && acc !== null
    width: parent.width
    spacing: Style.space(10)

    readonly property var acc: root.currentAccount()
    readonly property var accMetrics: acc && acc.metrics ? acc.metrics : []
    readonly property bool isApiKeyAuth: acc && (acc.authMethod === "api_key" || acc.authMethod === "org_token")
    readonly property bool isHubAcc: !!(acc && acc.hubId)

    // Account Header
    BorderSurface {
      width: parent.width
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      implicitHeight: detHeadCol.implicitHeight + Style.space(18)

      Column {
        id: detHeadCol
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.margins: Style.space(10)
        spacing: Style.space(8)

        Row {
          width: parent.width
          spacing: Style.space(8)

          BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: detailCol.acc ? (detailCol.acc.icon || "omameter.svg") : "omameter.svg"
            iconSize: Style.space(18)
            tintColor: root.foreground
            badgeBackground: root.background
            fontFamily: root.fontFamily
            isHub: detailCol.isHubAcc
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            PlainText {
              text: detailCol.acc ? String(detailCol.acc.displayName || detailCol.acc.kind) : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }

            PlainText {
              text: detailCol.acc
                ? ((detailCol.acc.plan ? detailCol.acc.plan + " · " : "") + Model.authMethodLabel(detailCol.acc.authMethod, root.strings))
                : ""
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.65)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        // Error message banner
        PlainText {
          visible: !!(detailCol.acc && detailCol.acc.error)
          width: parent.width
          text: detailCol.acc ? Model.cleanText(detailCol.acc.error || "", 260) : ""
          color: (detailCol.acc && Model.isTransientErrorKind(detailCol.acc.errorKind))
            ? (root.palette.warning || "#e59b3c")
            : (root.palette.critical || root.urgent)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        // Masked label shown in place of rename field while Privacy Mode is active
        PlainText {
          visible: root.privacyMode && !!(detailCol.acc && detailCol.acc.accountName)
          width: parent.width
          text: detailCol.acc ? Model.displayAccountName(detailCol.acc.accountName, { privacyMode: true }) : ""
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.72)
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
        }

        // Rename row (hidden in Privacy Mode so account email/label is never exposed in plaintext)
        FormField {
          id: detailRenameInput
          visible: !root.privacyMode
          width: parent.width
          text: root.drafts.rename
          buttonText: root.t("accounts.save", "Save")
          buttonEnabled: Navigation.canSubmit(root.nav, "rename")
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          onEdited: function(value) { root.dispatch({ type: "setDraft", field: "rename", value: value }) }
          onAccepted: root.dispatch({ type: "submitRename" })
          onButtonClicked: root.dispatch({ type: "submitRename" })
        }

        // Re-auth / Replace Key / Refresh actions
        Flow {
          width: parent.width
          spacing: Style.space(6)

          Button {
            visible: !detailCol.isHubAcc && !detailCol.isApiKeyAuth
            text: root.t("accounts.signInAgain", "Sign In Again")
            bordered: true
            selected: !!(detailCol.acc && detailCol.acc.error && Model.isAuthErrorKind(detailCol.acc.errorKind))
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: {
              if (!detailCol.acc) return
              root.dispatch({ type: "beginAuth", kind: detailCol.acc.kind, method: detailCol.acc.authMethod || "oauth", accountId: detailCol.acc.id, domain: detailCol.acc.domain || null })
            }
          }

          Button {
            visible: !detailCol.isHubAcc && detailCol.isApiKeyAuth
            text: root.t("accounts.replaceKey", "Replace Key")
            bordered: true
            selected: !!(detailCol.acc && detailCol.acc.error && Model.isAuthErrorKind(detailCol.acc.errorKind))
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: {
              if (!detailCol.acc) return
              root.dispatch({ type: "openApiKeyForm", kind: detailCol.acc.kind, method: detailCol.acc.authMethod, accountId: detailCol.acc.id })
            }
          }

          Button {
            text: root.t("card.refresh", "Refresh")
            iconText: "󰑐"
            bordered: true
            selected: !!(detailCol.acc && detailCol.acc.error && Model.isTransientErrorKind(detailCol.acc.errorKind))
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: if (root.service && detailCol.acc) root.service.refreshAccount(detailCol.acc.id, true)
          }
        }
      }
    }

    // Metrics visibility, pinning, and reorder
    Column {
      visible: detailCol.accMetrics.length > 0
      width: parent.width
      spacing: Style.space(6)

      PanelSectionHeader {
        text: root.t("accounts.metrics", "Metrics")
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Repeater {
        model: detailCol.accMetrics
        delegate: BorderSurface {
          required property var modelData
          required property int index
          width: parent.width
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.foreground, root.accent)
          borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
          implicitHeight: mRow.implicitHeight + Style.space(10)

          Row {
            id: mRow
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            spacing: Style.space(6)

            PlainText {
              anchors.verticalCenter: parent.verticalCenter
              width: Math.max(80, mRow.width - mCtrlRow.implicitWidth - Style.space(8))
              text: String(modelData.title || modelData.key || "")
              color: modelData.hidden
                ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.45)
                : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Row {
              id: mCtrlRow
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(3)

              PanelActionButton {
                visible: detailCol.accMetrics.length > 1
                enabled: index > 0
                opacity: enabled ? 1.0 : 0.25
                iconText: "↑"
                size: Style.space(20)
                fontSize: Style.font.caption
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: if (root.service && detailCol.acc) root.service.moveMetric(detailCol.acc.id, modelData.key, -1)
              }

              PanelActionButton {
                visible: detailCol.accMetrics.length > 1
                enabled: index < detailCol.accMetrics.length - 1
                opacity: enabled ? 1.0 : 0.25
                iconText: "↓"
                size: Style.space(20)
                fontSize: Style.font.caption
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: if (root.service && detailCol.acc) root.service.moveMetric(detailCol.acc.id, modelData.key, 1)
              }

              PanelActionButton {
                enabled: !modelData.hidden
                iconText: modelData.pinned ? "󰐄" : "󰐃"
                tooltipText: Model.autoTextSafe(modelData.pinned ? root.t("card.unpin", "Unpin") : root.t("card.pin", "Pin"), false)
                size: Style.space(20)
                fontSize: Style.font.caption
                foreground: modelData.pinned ? root.accent : root.foreground
                hoverColor: root.accent
                fontFamily: root.fontFamily
                onClicked: if (root.service) root.service.setMetricPinned([modelData.metricId], !modelData.pinned)
              }

              ToggleSwitch {
                anchors.verticalCenter: parent.verticalCenter
                trackHeight: 18
                checked: !modelData.hidden
                foreground: root.foreground
                accent: root.accent
                onToggled: if (root.service) root.service.setMetricHidden([modelData.metricId], !modelData.hidden)
              }
            }
          }
        }
      }
    }

    PanelSeparator { foreground: root.foreground }

    // Destructive Remove Account (two-step)
    ConfirmStep {
      anchors.right: parent.right
      confirming: root.nav.confirmRemoveAccount
      actionText: root.t("accounts.removeAccount", "Remove Account")
      cancelText: root.t("accounts.cancel", "Cancel")
      foreground: root.foreground
      accent: root.accent
      dangerColor: root.palette.critical || root.urgent
      dangerAccent: root.urgent
      fontFamily: root.fontFamily
      onActionClicked: root.dispatch({ type: "askRemoveAccount", confirm: true })
      onCancelled: root.dispatch({ type: "askRemoveAccount", confirm: false })
      onConfirmed: root.dispatch({ type: "confirmRemoveAccount" })
    }
  }

  // ==================== HUB DETAIL MODE ====================
  Column {
    id: hubDetCol
    visible: root.subPage === "hub_detail" && hub !== null
    width: parent.width
    spacing: Style.space(10)

    readonly property var hub: root.currentHub()

    BorderSurface {
      width: parent.width
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      implicitHeight: hdInner.implicitHeight + Style.space(18)

      Column {
        id: hdInner
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.margins: Style.space(10)
        spacing: Style.space(8)

        Row {
          spacing: Style.space(8)

          BrandMark {
            anchors.verticalCenter: parent.verticalCenter
            icon: "cliproxyapi.svg"
            iconSize: Style.space(18)
            tintColor: root.foreground
            fontFamily: root.fontFamily
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            PlainText {
              text: hubDetCol.hub ? String(hubDetCol.hub.host || "") : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }

            PlainText {
              text: hubDetCol.hub ? String(hubDetCol.hub.baseUrl || "") : ""
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.62)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        PlainText {
          visible: !!(hubDetCol.hub && hubDetCol.hub.lastError)
          width: parent.width
          text: hubDetCol.hub ? Model.cleanText(hubDetCol.hub.lastError || "", 260) : ""
          color: root.palette.critical || root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        PlainText {
          visible: !!(hubDetCol.hub && Number(hubDetCol.hub.unsupportedCount) > 0)
          width: parent.width
          text: hubDetCol.hub
            ? root.tn("accounts.unsupportedLogins", Number(hubDetCol.hub.unsupportedCount), "%1 unsupported logins on hub").arg(Number(hubDetCol.hub.unsupportedCount))
            : ""
          color: root.palette.warning || "#e59b3c"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // Replace Management Key
        FormField {
          id: replaceHubKeyInput
          width: parent.width
          label: root.t("accounts.replaceKey", "Replace Key") + " (" + root.t("accounts.hubKeyLabel", "Management Key") + ")"
          password: true
          placeholderText: "••••••••"
          text: root.drafts.replaceHubKey
          buttonText: root.submitLabel("replaceHubKey", root.t("accounts.save", "Save"))
          buttonEnabled: Navigation.canSubmit(root.nav, "replaceHubKey")
          errorText: root.formErrorText("replaceHubKey")
          foreground: root.foreground
          accent: root.accent
          errorColor: root.palette.critical || root.urgent
          fontFamily: root.fontFamily
          onEdited: function(value) { root.dispatch({ type: "setDraft", field: "replaceHubKey", value: value }) }
          onAccepted: root.dispatch({ type: "submitReplaceHubKey" })
          onButtonClicked: root.dispatch({ type: "submitReplaceHubKey" })
        }

        Button {
          text: root.t("card.refresh", "Refresh")
          iconText: "󰑐"
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: if (root.service) root.service.syncHubs(true)
        }
      }
    }

    ConfirmStep {
      anchors.right: parent.right
      confirming: root.nav.confirmRemoveHub
      actionText: root.t("accounts.removeHub", "Remove Hub")
      cancelText: root.t("accounts.cancel", "Cancel")
      foreground: root.foreground
      accent: root.accent
      dangerColor: root.palette.critical || root.urgent
      dangerAccent: root.urgent
      fontFamily: root.fontFamily
      onActionClicked: root.dispatch({ type: "askRemoveHub", confirm: true })
      onCancelled: root.dispatch({ type: "askRemoveHub", confirm: false })
      onConfirmed: root.dispatch({ type: "confirmRemoveHub" })
    }
  }
}
