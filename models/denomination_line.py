# -*- coding: utf-8 -*-
"""Denomination Line — shared denomination definitions and counting line model.

Used by till counts, safe counts, and change slips to track quantities
of each denomination (bills, rolled coins, loose coins).
"""
from odoo import api, fields, models


# ── shared constants ────────────────────────────────────────────
DENOMINATION_SELECTION = [
    ('100', '$100 Bill'),
    ('50', '$50 Bill'),
    ('20', '$20 Bill'),
    ('10', '$10 Bill'),
    ('5', '$5 Bill'),
    ('2', '$2 Bill'),
    ('1', '$1 Bill'),
    ('half_dollar', 'Half Dollar'),
    ('quarter', 'Quarter'),
    ('dime', 'Dime'),
    ('nickel', 'Nickel'),
    ('penny', 'Penny'),
]

DENOMINATION_VALUES = {
    '100': 100.00,
    '50': 50.00,
    '20': 20.00,
    '10': 10.00,
    '5': 5.00,
    '2': 2.00,
    '1': 1.00,
    'half_dollar': 0.50,
    'quarter': 0.25,
    'dime': 0.10,
    'nickel': 0.05,
    'penny': 0.01,
}

CATEGORY_SELECTION = [
    ('bill', 'Bills'),
    ('rolled', 'Rolled Coin'),
    ('loose', 'Loose Coin'),
]

# Standard coin roll quantities
ROLL_QUANTITIES = {
    'half_dollar': 20,
    'quarter': 40,
    'dime': 50,
    'nickel': 40,
    'penny': 50,
}


class ElksDenominationLine(models.Model):
    """A single denomination counting line."""

    _name = "elks.denomination.line"
    _description = "Denomination Line"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)

    # ── polymorphic parent links ────────────────────────────────
    till_count_id = fields.Many2one(
        "elks.till.count", ondelete="cascade", index=True,
    )
    safe_count_id = fields.Many2one(
        "elks.safe.count", ondelete="cascade", index=True,
    )
    change_slip_id = fields.Many2one(
        "elks.change.slip", ondelete="cascade", index=True,
    )

    # ── denomination data ───────────────────────────────────────
    category = fields.Selection(
        CATEGORY_SELECTION, string="Category", required=True,
        default='bill',
    )
    denomination = fields.Selection(
        DENOMINATION_SELECTION, required=True, string="Denomination",
    )
    quantity = fields.Integer("Qty", default=0)

    face_value = fields.Float(
        "Face Value", compute="_compute_face_value",
        store=True, digits=(10, 2),
    )
    subtotal = fields.Monetary(
        "Subtotal", compute="_compute_subtotal",
        store=True, currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    @api.depends("denomination")
    def _compute_face_value(self):
        for line in self:
            line.face_value = DENOMINATION_VALUES.get(line.denomination, 0.0)

    @api.depends("face_value", "quantity")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.face_value * (line.quantity or 0)
