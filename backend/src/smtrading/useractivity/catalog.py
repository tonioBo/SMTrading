"""The actions of the activity log: each unsafe (method, route template) of the API has a
`category.verb` key and an Italian label. A route missing here is still recorded (as
`other.request`), but the route coverage test fails until it gets its entry."""

CATEGORIES: dict[str, str] = {
    "auth": "Accesso",
    "users": "Utenti",
    "portfolios": "Portafogli",
    "agents": "Agenti",
    "fees": "Commissioni",
    "risk": "Rischio e margine",
    "goals": "Obiettivi",
    "emergency": "Emergenza",
    "connectors": "Connettori",
    "brokers": "Broker",
    "live": "Denaro reale",
    "markets": "Mercati",
    "telegram": "Notifiche",
    "paperclip": "Paperclip",
    "system": "Sistema",
    "other": "Altro",
}

LABELS: dict[str, str] = {
    # access
    "auth.login": "Accesso",
    "auth.login_failed": "Accesso non riuscito",
    "auth.login_locked": "Accesso bloccato (troppi tentativi)",
    "auth.logout": "Uscita",
    "auth.sessions_revoke": "Chiusura delle altre sessioni",
    "auth.totp_setup": "Configurazione TOTP",
    "auth.totp_enable": "Attivazione TOTP",
    "auth.totp_disable": "Disattivazione TOTP",
    "auth.totp_lockout": "TOTP di conferma bloccato (troppi codici errati)",
    "auth.password_change": "Cambio password",
    "auth.password_forced_change": "Cambio della password temporanea",
    # users
    "users.create": "Creazione utente",
    "users.set_role": "Cambio ruolo utente",
    "users.disable": "Disattivazione utente",
    "users.enable": "Riattivazione utente",
    "users.reset_password": "Reimpostazione password utente",
    "users.reset_totp": "Azzeramento TOTP utente",
    # portfolios
    "portfolios.create": "Creazione portafoglio",
    "portfolios.update": "Modifica portafoglio",
    "portfolios.order_preview": "Anteprima ordine manuale",
    "portfolios.order_place": "Ordine manuale",
    "portfolios.order_cancel": "Annullamento ordine",
    "portfolios.deposit": "Deposito",
    "portfolios.withdraw": "Prelievo",
    "portfolios.journal_reverse": "Storno movimento",
    "portfolios.reset": "Azzeramento portafoglio",
    "portfolios.archive": "Archiviazione portafoglio",
    "portfolios.unarchive": "Ripristino portafoglio archiviato",
    "portfolios.broker_link": "Collegamento portafoglio al broker",
    # agents
    "agents.create": "Creazione agente",
    "agents.rotate_token": "Rigenerazione token agente",
    "agents.update": "Modifica agente",
    "agents.set_portfolios": "Portafogli dell'agente",
    "agents.docs_export": "Esportazione istruzioni agenti",
    "agents.doc_save": "Modifica istruzioni agente",
    "agents.doc_restore": "Ripristino versione istruzioni",
    "agents.watchlist_remove_symbol": "Rimozione simbolo dalla watchlist",
    "agents.watchlist_delete": "Eliminazione watchlist",
    "agents.alert_cancel": "Annullamento avviso",
    # fees
    "fees.rule_create": "Creazione regola commissioni",
    "fees.rule_update": "Modifica regola commissioni",
    "fees.rule_deactivate": "Disattivazione regola commissioni",
    "fees.rule_simulate": "Simulazione regola commissioni",
    "fees.simulate": "Simulazione commissioni",
    "fees.preset_apply": "Commissioni del broker applicate al portafoglio",
    # risk
    "risk.profile_create": "Creazione profilo di rischio",
    "risk.profile_update": "Modifica profilo di rischio",
    "risk.interest_rates": "Modifica tassi di interesse",
    # goals
    "goals.create": "Creazione obiettivo",
    "goals.update": "Modifica obiettivo",
    "goals.deactivate": "Disattivazione obiettivo",
    # emergency
    "emergency.kill_switch": "Kill switch",
    "emergency.halt_set": "Sospensione del trading",
    "emergency.halt_clear": "Ripresa del trading",
    "emergency.live_disarm": "Disattivazione denaro reale",
    # connectors
    "connectors.update": "Modifica connettore",
    "connectors.test": "Test connettore",
    "connectors.priorities": "Priorità dei fornitori di dati",
    # brokers
    "brokers.link_create": "Creazione collegamento broker",
    "brokers.link_update": "Modifica collegamento broker",
    "brokers.link_test": "Test collegamento broker",
    "brokers.reconcile": "Riconciliazione con il broker",
    # live
    "live.link_create": "Creazione collegamento denaro reale",
    "live.keys": "Nuove chiavi denaro reale",
    "live.test": "Test collegamento denaro reale",
    "live.dry_run": "Prova senza ordini (dry run)",
    "live.arm": "Attivazione denaro reale",
    "live.portfolio_link": "Collegamento portafoglio al denaro reale",
    "live.resume": "Ripresa portafoglio reale",
    "live.flatten": "Chiusura posizioni reali",
    # markets
    "markets.track": "Monitoraggio simboli",
    "markets.untrack": "Fine monitoraggio simboli",
    "markets.auto_add": "Aggiunta automatica strumenti",
    "markets.instrument_create": "Creazione strumento",
    "markets.instrument_update": "Modifica strumento",
    "markets.closure_create": "Creazione chiusura straordinaria",
    "markets.closure_delete": "Eliminazione chiusura straordinaria",
    # telegram
    "telegram.config": "Configurazione Telegram",
    "telegram.detect_chats": "Ricerca chat Telegram",
    "telegram.test": "Messaggio di prova Telegram",
    "telegram.paused": "Pausa notifiche Telegram",
    "telegram.retry_failed": "Nuovo invio dei messaggi falliti",
    "telegram.retry": "Nuovo invio di un messaggio",
    # paperclip
    "paperclip.config": "Configurazione Paperclip",
    "paperclip.verify": "Verifica lettura Paperclip",
    "paperclip.baseline": "Riconferma regole di isolamento Paperclip",
    # system
    "system.backup": "Download del backup",
    "system.app_settings": "Impostazioni dell'app",
    # other
    "other.request": "Altra azione",
    "other.unknown": "Richiesta a una route inesistente",
}

ROUTE_ACTIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/login"): "auth.login",
    ("POST", "/api/auth/logout"): "auth.logout",
    ("POST", "/api/auth/sessions/revoke-others"): "auth.sessions_revoke",
    ("POST", "/api/auth/totp/setup"): "auth.totp_setup",
    ("POST", "/api/auth/totp/enable"): "auth.totp_enable",
    ("POST", "/api/auth/totp/disable"): "auth.totp_disable",
    ("POST", "/api/auth/password"): "auth.password_change",
    ("POST", "/api/users"): "users.create",
    ("PUT", "/api/users/{user_id}/role"): "users.set_role",
    ("POST", "/api/users/{user_id}/disable"): "users.disable",
    ("POST", "/api/users/{user_id}/enable"): "users.enable",
    ("POST", "/api/users/{user_id}/reset-password"): "users.reset_password",
    ("POST", "/api/users/{user_id}/reset-totp"): "users.reset_totp",
    ("POST", "/api/portfolios"): "portfolios.create",
    ("PATCH", "/api/portfolios/{portfolio_id}"): "portfolios.update",
    ("POST", "/api/portfolios/{portfolio_id}/orders/preview"): "portfolios.order_preview",
    ("POST", "/api/portfolios/{portfolio_id}/orders"): "portfolios.order_place",
    ("POST", "/api/orders/{order_id}/cancel"): "portfolios.order_cancel",
    ("POST", "/api/portfolios/{portfolio_id}/deposit"): "portfolios.deposit",
    ("POST", "/api/portfolios/{portfolio_id}/withdraw"): "portfolios.withdraw",
    ("POST", "/api/journals/{journal_id}/reverse"): "portfolios.journal_reverse",
    ("POST", "/api/portfolios/{portfolio_id}/reset"): "portfolios.reset",
    ("POST", "/api/portfolios/{portfolio_id}/archive"): "portfolios.archive",
    ("POST", "/api/portfolios/{portfolio_id}/unarchive"): "portfolios.unarchive",
    ("PUT", "/api/portfolios/{portfolio_id}/broker-link"): "portfolios.broker_link",
    ("POST", "/api/agents"): "agents.create",
    ("POST", "/api/agents/{agent_id}/rotate"): "agents.rotate_token",
    ("PATCH", "/api/agents/{agent_id}"): "agents.update",
    ("PUT", "/api/agents/{agent_id}/portfolios"): "agents.set_portfolios",
    ("POST", "/api/agent-docs/export"): "agents.docs_export",
    ("PUT", "/api/agent-docs/{slug}"): "agents.doc_save",
    ("POST", "/api/agent-docs/{slug}/restore/{version}"): "agents.doc_restore",
    ("DELETE", "/api/agents/{agent_id}/watchlists/{watchlist_id}/items/{symbol}"):
        "agents.watchlist_remove_symbol",
    ("DELETE", "/api/agents/{agent_id}/watchlists/{watchlist_id}"): "agents.watchlist_delete",
    ("POST", "/api/agents/{agent_id}/alerts/{alert_id}/cancel"): "agents.alert_cancel",
    ("POST", "/api/fee-rules"): "fees.rule_create",
    ("PATCH", "/api/fee-rules/{rule_id}"): "fees.rule_update",
    ("DELETE", "/api/fee-rules/{rule_id}"): "fees.rule_deactivate",
    ("POST", "/api/fee-rules/simulate"): "fees.rule_simulate",
    ("POST", "/api/fees/simulate"): "fees.simulate",
    ("POST", "/api/portfolios/{portfolio_id}/fee-rule/apply-preset"): "fees.preset_apply",
    ("POST", "/api/risk-profiles"): "risk.profile_create",
    ("PATCH", "/api/risk-profiles/{profile_id}"): "risk.profile_update",
    ("PUT", "/api/interest-rates"): "risk.interest_rates",
    ("PUT", "/api/settings/app"): "system.app_settings",
    ("POST", "/api/goals"): "goals.create",
    ("PATCH", "/api/goals/{goal_id}"): "goals.update",
    ("DELETE", "/api/goals/{goal_id}"): "goals.deactivate",
    ("POST", "/api/kill-switch"): "emergency.kill_switch",
    ("POST", "/api/halts"): "emergency.halt_set",
    ("POST", "/api/halts/{halt_id}/clear"): "emergency.halt_clear",
    ("POST", "/api/live-links/{link_id}/disarm"): "emergency.live_disarm",
    ("PATCH", "/api/connectors/{name}"): "connectors.update",
    ("POST", "/api/connectors/{name}/test"): "connectors.test",
    ("PUT", "/api/provider-priorities"): "connectors.priorities",
    ("POST", "/api/broker-links"): "brokers.link_create",
    ("PATCH", "/api/broker-links/{link_id}"): "brokers.link_update",
    ("POST", "/api/broker-links/{link_id}/test"): "brokers.link_test",
    ("POST", "/api/broker-links/{link_id}/reconcile"): "brokers.reconcile",
    ("POST", "/api/live-links"): "live.link_create",
    ("PUT", "/api/live-links/{link_id}/keys"): "live.keys",
    ("POST", "/api/live-links/{link_id}/test"): "live.test",
    ("POST", "/api/live-links/{link_id}/dry-run"): "live.dry_run",
    ("POST", "/api/live-links/{link_id}/arm"): "live.arm",
    ("PUT", "/api/portfolios/{portfolio_id}/live-link"): "live.portfolio_link",
    ("POST", "/api/portfolios/{portfolio_id}/live-resume"): "live.resume",
    ("POST", "/api/portfolios/{portfolio_id}/live-flatten"): "live.flatten",
    ("POST", "/api/marketdata/track"): "markets.track",
    ("POST", "/api/marketdata/untrack"): "markets.untrack",
    ("PUT", "/api/instruments/auto-add"): "markets.auto_add",
    ("POST", "/api/instruments"): "markets.instrument_create",
    ("PATCH", "/api/instruments/{symbol}"): "markets.instrument_update",
    ("POST", "/api/market-closures"): "markets.closure_create",
    ("DELETE", "/api/market-closures/{closure_id}"): "markets.closure_delete",
    ("POST", "/api/system/backup"): "system.backup",
    ("PUT", "/api/telegram/config"): "telegram.config",
    ("POST", "/api/telegram/detect-chats"): "telegram.detect_chats",
    ("POST", "/api/telegram/test"): "telegram.test",
    ("PUT", "/api/telegram/paused"): "telegram.paused",
    ("POST", "/api/telegram/outbox/retry-failed"): "telegram.retry_failed",
    ("POST", "/api/telegram/outbox/{outbox_id}/retry"): "telegram.retry",
    ("PUT", "/api/paperclip/config"): "paperclip.config",
    ("POST", "/api/paperclip/verify"): "paperclip.verify",
    ("POST", "/api/paperclip/baseline"): "paperclip.baseline",
}

LOGIN = ("POST", "/api/auth/login")


def action_for(method: str, route: str | None, outcome: str, error_code: str | None,
               override: str | None = None) -> str:
    """The action of a request: the route's, the login's by outcome (failed, locked), or
    `override` (set by the handler, e.g. the forced password change) on success."""
    if route is None:
        return "other.unknown"
    if (method, route) == LOGIN and outcome != "ok":
        return "auth.login_locked" if error_code == "RATE_LIMITED" else "auth.login_failed"
    if override is not None and outcome == "ok":
        return override
    return ROUTE_ACTIONS.get((method, route), "other.request")


def category(action: str) -> str:
    return action.split(".", 1)[0]


def label(action: str) -> str:
    return LABELS.get(action, action)
