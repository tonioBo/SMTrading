"""Imports every ORM model module so Base.metadata is complete (migrations, tests)."""

import smtrading.activity.models
import smtrading.admin.models
import smtrading.agentdocs.models
import smtrading.agents.models
import smtrading.autorules.tables
import smtrading.backtest.models
import smtrading.brokers.live.models
import smtrading.brokers.models
import smtrading.campaigns.models
import smtrading.connectors.models
import smtrading.engine.models
import smtrading.fees.models
import smtrading.goals.models
import smtrading.instruments.closures
import smtrading.instruments.models
import smtrading.intraday.models
import smtrading.ledger.models
import smtrading.marketsportal.models
import smtrading.neurograph.models
import smtrading.paperclip.models
import smtrading.portfolios.models
import smtrading.positions.models
import smtrading.reconciliation.models
import smtrading.risk.models
import smtrading.telegram.models
import smtrading.tv.models
import smtrading.useractivity.models
import smtrading.watch.models  # noqa: F401
