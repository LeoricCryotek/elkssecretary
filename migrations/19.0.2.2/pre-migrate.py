# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.2.

`elks.area.pnl.period_year` is changing from Integer to Selection
(stored as varchar).  PostgreSQL needs an explicit USING clause to
cast int → text safely, which Odoo's auto-schema sync doesn't do.

Fresh-install / pre-19.0.2.0 upgrade: elks_area_pnl doesn't exist yet,
so there's nothing to convert — schema sync will create the column
already as varchar.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Guard: this migration only matters if the area-P&L table already
    # exists from a prior install of 19.0.2.0/.1.  See the standing-rule
    # comment in migrations/19.0.2.8/pre-migrate.py.
    cr.execute("SELECT to_regclass('public.elks_area_pnl')")
    if cr.fetchone()[0] is None:
        _logger.info(
            "elkssecretary 19.0.2.2: elks_area_pnl does not exist "
            "yet — skipping period_year type conversion"
        )
        return

    # Belt-and-suspenders: even if the table is there, the column
    # might already be varchar (e.g. someone manually adjusted, or a
    # mid-version reinstall).  Only ALTER if it's still an integer
    # type — the information_schema query returns 0 rows when the
    # column doesn't exist, so the conditional also handles that.
    cr.execute("""
        SELECT data_type
          FROM information_schema.columns
         WHERE table_name = 'elks_area_pnl'
           AND column_name = 'period_year'
    """)
    row = cr.fetchone()
    if row and row[0] in ('integer', 'bigint', 'smallint'):
        _logger.info(
            "elkssecretary 19.0.2.2: converting "
            "elks_area_pnl.period_year %s → text", row[0],
        )
        cr.execute("""
            ALTER TABLE elks_area_pnl
            ALTER COLUMN period_year TYPE varchar
            USING period_year::text
        """)
    else:
        _logger.info(
            "elkssecretary 19.0.2.2: period_year is not an integer "
            "type (current: %s) — no conversion needed",
            row[0] if row else "missing",
        )
