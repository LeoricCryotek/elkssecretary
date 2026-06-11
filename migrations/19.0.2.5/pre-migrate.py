# -*- coding: utf-8 -*-
"""Pre-migration for elkssecretary 19.0.2.5.

product_template.elks_area_id (Many2one) is being replaced with
elks_area_ids (Many2many).  We need to:

1. Create the new M2m link table if it doesn't exist yet.
2. Copy existing single-area assignments into the M2m.
3. Drop the obsolete single-area column afterward (Odoo's auto-schema
   would do it but we want it gone before any new reads happen).

Fresh-install / pre-19.0.2.0 upgrade: product_template never carried
the old elks_area_id column, so the column-existence check skips out.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Defensive: product_template comes from a hard dependency
    # ("product"), so this should always be present.  Still, a
    # to_regclass guard makes the intent explicit and matches the
    # standing rule documented in migrations/19.0.2.8/pre-migrate.py.
    cr.execute("SELECT to_regclass('public.product_template')")
    if cr.fetchone()[0] is None:
        _logger.warning(
            "elkssecretary 19.0.2.5: product_template not found — "
            "skipping area-tag migration (this indicates a broken "
            "dependency chain, not a normal fresh install)"
        )
        return

    # Skip cleanly if the old column was never created.  This is the
    # normal fresh-install / pre-19.0.2.0-upgrade path: no legacy
    # column existed, so there's nothing to migrate.
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'product_template'
           AND column_name = 'elks_area_id'
    """)
    if not cr.fetchone():
        _logger.info(
            "elkssecretary 19.0.2.5: no legacy elks_area_id column "
            "to migrate (fresh install or upgrade from pre-19.0.2.0)"
        )
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
    _logger.info(
        "elkssecretary 19.0.2.5: migrated %d product-area tag(s) "
        "from elks_area_id → elks_area_ids", moved,
    )

    # Drop the old column so Odoo's schema sync doesn't try to keep it.
    cr.execute("""
        ALTER TABLE product_template
        DROP COLUMN IF EXISTS elks_area_id
    """)
