# -*- coding: utf-8 -*-
"""Tag products with one or more Lodge Areas.

[Human]
    Each product (think "Burger", "Vodka Tonic", "Non-Member $1
    Surcharge") can be tagged with the areas it's sold or consumed
    in.  Then the P&L form's "Add Sales/COGS from Products" buttons
    and the Clover CSV import know which area's products to pull.
    A product can belong to multiple areas if it's sold in both
    (e.g. a soda served in both the Kitchen and the Lounge).

[AI]
    • Inherits product.template (not product.product) — same Many2many
      applies to all variants.
    • Field name: elks_area_ids (Many2many to elks.area).
    • Relation table: elks_area_product_template_rel
        (product_template_id, elks_area_id) — explicit name so a future
        cleanup migration can target it precisely.
    • Searches that reference it (in models/area_pnl.py):
        ('product_tmpl_id.elks_area_ids', 'in', [self.area_id.id])
      — note 'in [id]' form because we want products whose list
      contains the area, not exact-equals.
    • Migration: migrations/19.0.2.5/pre-migrate.py copies values from
      the original Many2one (elks_area_id, deprecated) into this
      Many2many's relation table and drops the old column.
"""
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    elks_area_ids = fields.Many2many(
        "elks.area",
        relation="elks_area_product_template_rel",
        column1="product_template_id",
        column2="elks_area_id",
        string="Lodge Areas",
        help="Lodge operating areas this product is sold or consumed "
             "in.  Drives the Area P&L's product-pull actions.  A "
             "product can belong to multiple areas.",
    )
