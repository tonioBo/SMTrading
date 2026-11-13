"""Imports every ORM model module so Base.metadata is complete (migrations, tests)."""

import smtrading.admin.models
import smtrading.agents.models
import smtrading.connectors.models
import smtrading.engine.models
import smtrading.fees.models
import smtrading.instruments.models
import smtrading.ledger.models
import smtrading.portfolios.models
import smtrading.positions.models
import smtrading.risk.models  # noqa: F401
