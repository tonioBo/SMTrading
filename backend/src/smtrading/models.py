"""Imports every ORM model module so Base.metadata is complete (migrations, tests)."""

import smtrading.activity.models
import smtrading.admin.models
import smtrading.agents.models
import smtrading.backtest.models
import smtrading.brokers.live.models
import smtrading.brokers.models
import smtrading.connectors.models
import smtrading.engine.models
import smtrading.fees.models
import smtrading.instruments.models
import smtrading.ledger.models
import smtrading.portfolios.models
import smtrading.positions.models
import smtrading.risk.models
import smtrading.telegram.models
import smtrading.watch.models  # noqa: F401
