# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.2.

`elks.area.pnl.period_year` is changing from Integer to Selection
(stored as varchar).  PostgreSQL needs an explicit USING clause to
cast int → text safely, which Odoo's auto-schema sync doesn't do.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT data_type
          FROM information_schema.columns
         WHERE table_name = 'elks_area_pnl'
           AND column_name = 'period_year'
    """)
    row = cr.fetchone()
    if row and row[0] in ('integer', 'bigint', 'smallint'):
        _logger.info(
            "elkssecretary: converting elks_area_pnl.period_year "
            "%s → text", row[0],
        )
        cr.execute("""
            ALTER TABLE elks_area_pnl
            ALTER COLUMN period_year TYPE varchar
            USING period_year::text
        """)
