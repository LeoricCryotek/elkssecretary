# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.5.

product_template.elks_area_id (Many2one) is being replaced with
elks_area_ids (Many2many).  We need to:

1. Create the new M2m link table if it doesn't exist yet.
2. Copy existing single-area assignments into the M2m.
3. Drop the obsolete single-area column afterward (Odoo's auto-schema
   would do it but we want it gone before any new reads happen).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Skip cleanly if the old column was never created.
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'product_template'
           AND column_name = 'elks_area_id'
    """)
    if not cr.fetchone():
        _logger.info("elkssecretary: no legacy elks_area_id column to "
                     "migrate")
        return

    # Make sure the new M2m table exists with the right shape.
    cr.execute("""
        CREATE TABLE IF NOT EXISTS elks_area_product_template_rel (
            product_template_id integer NOT NULL,
            elks_area_id        integer NOT NULL,
            PRIMARY KEY (product_template_id, elks_area_id)
        )
    """)

    # Copy single-area tags into the new M2m, skipping NULLs and dupes.
    cr.execute("""
        INSERT INTO elks_area_product_template_rel
            (product_template_id, elks_area_id)
        SELECT id, elks_area_id
          FROM product_template
         WHERE elks_area_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    moved = cr.rowcount
    _logger.info("elkssecretary: migrated %d product-area tag(s) from "
                 "elks_area_id → elks_area_ids", moved)

    # Drop the old column so Odoo's schema sync doesn't try to keep it.
    cr.execute("""
        ALTER TABLE product_template
        DROP COLUMN IF EXISTS elks_area_id
    """)
