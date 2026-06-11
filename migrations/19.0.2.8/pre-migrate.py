# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.8.

The old `_uniq_period` SQL constraint on elks_area_pnl is being
replaced with a Python @api.constrains that also takes event_id into
account.  Drop the obsolete constraint here so the new model load
doesn't conflict with it.

Standing rule for this module's migrations
==========================================
This module is deployed to databases at very different starting
versions.  In particular, the Area P&L feature (elks.area*, plus
hr_employee.elks_area_id) was added at 19.0.2.0 — databases installed
before that have none of those tables yet.

Pre-migrate runs BEFORE Odoo's schema sync.  Any ALTER TABLE / DROP
CONSTRAINT / UPDATE / column reference must therefore tolerate the
target object not existing yet:

  - Tables: guard with `to_regclass('public.<table>')`.
  - Columns: query information_schema.columns for the (table, column).
  - "Doesn't exist yet" == "schema sync will create it next; skip".

`DROP CONSTRAINT IF EXISTS` only guards the constraint, NOT the table:
on a missing table, ALTER TABLE itself raises UndefinedTable.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    # The constraint we want to drop only existed on databases where
    # elks_area_pnl was created by 19.0.2.0–19.0.2.7.  If the table
    # itself doesn't exist (fresh install, or upgrade from a build
    # before the Area P&L feature was introduced), schema sync will
    # create the table later in this same upgrade pass without the
    # constraint, so nothing to do here.
    cr.execute("SELECT to_regclass('public.elks_area_pnl')")
    if cr.fetchone()[0] is None:
        _logger.info(
            "elkssecretary 19.0.2.8: elks_area_pnl does not exist "
            "yet — skipping uniq_period drop (fresh install or "
            "pre-19.0.2.0 upgrade)"
        )
        return
    cr.execute("""
        ALTER TABLE elks_area_pnl
        DROP CONSTRAINT IF EXISTS elks_area_pnl_uniq_period
    """)
    _logger.info(
        "elkssecretary 19.0.2.8: dropped legacy uniq_period "
        "constraint (replaced by Python @api.constrains)"
    )
