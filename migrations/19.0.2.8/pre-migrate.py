# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.8.

The old `_uniq_period` SQL constraint on elks_area_pnl is being
replaced with a Python @api.constrains that also takes event_id into
account.  Drop the obsolete index/constraint here so the new model
load doesn't conflict with it.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE elks_area_pnl
        DROP CONSTRAINT IF EXISTS elks_area_pnl_uniq_period
    """)
    _logger.info("elkssecretary: dropped legacy uniq_period constraint "
                 "(replaced by Python @api.constrains)")
