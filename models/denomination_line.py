# -*- coding: utf-8 -*-
"""Denomination detail line for till and safe counts.

Each line represents a quantity of a single denomination (bills, rolled
coins, or loose coins).  The face value is looked up from a constant map
and the subtotal is computed automatically.
"""
from odoo import api, fields, models

# ── denomination catalogue ──────────────────────────────────────────
DENOMINATION_SELECTION = [
    # Bills
    ('bill_100', '$100 Bills'),
    ('bill_50', '$50 Bills'),
    ('bill_20', '$20 Bills'),
    ('bill_10', '$10 Bills'),
    ('bill_5', '$5 Bills'),
    ('bill_2', '$2 Bills'),
    ('bill_1', '$1 Bills'),
    # Rolled Coins
    ('roll_quarter', 'Quarters — Rolled ($10)'),
    ('roll_dime', 'Dimes — Rolled ($5)'),
    ('roll_nickel', 'Nickels — Rolled ($2)'),
    ('roll_penny', 'Pennies — Rolled ($0.50)'),
    # Loose Coins
    ('coin_dollar', '$1 Coins'),
    ('coin_half', '50¢ Pieces'),
    ('coin_quarter', 'Quarters'),
    ('coin_dime', 'Dimes'),
    ('coin_nickel', 'Nickels'),
    ('coin_penny', 'Pennies'),
]

DENOMINATION_VALUES = {
    'bill_100': 100.00,
    'bill_50': 50.00,
    'bill_20': 20.00,
    'bill_10': 10.00,
    'bill_5': 5.00,
    'bill_2': 2.00,
    'bill_1': 1.00,
    'roll_quarter': 10.00,
    'roll_dime': 5.00,
    'roll_nickel': 2.00,
    'roll_penny': 0.50,
    'coin_dollar': 1.00,
    'coin_half': 0.50,
    'coin_quarter': 0.25,
    'coin_dime': 0.10,
    'coin_nickel': 0.05,
    'coin_penny': 0.01,
}

DENOM_CATEGORY = {
    'bill_100': 'bills', 'bill_50': 'bills', 'bill_20': 'bills',
    'bill_10': 'bills', 'bill_5': 'bills', 'bill_2': 'bills',
    'bill_1': 'bills',
    'roll_quarter': 'rolled', 'roll_dime': 'rolled',
    'roll_nickel': 'rolled', 'roll_penny': 'rolled',
    'coin_dollar': 'loose', 'coin_half': 'loose',
    'coin_quarter': 'loose', 'coin_dime': 'loose',
    'coin_nickel': 'loose', 'coin_penny': 'loose',
}


class ElksDenominationLine(models.Model):
    """Single denomination quantity for a till or safe count."""

    _name = "elks.denomination.line"
    _description = "Denomination Line"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)

    denomination = fields.Selection(
        DENOMINATION_SELECTION, required=True, string="Denomination",
    )
    category = fields.Selection(
        [('bills', 'Bills'),
         ('rolled', 'Rolled Coins'),
         ('loose', 'Loose Coins')],
        compute="_compute_category", store=True,
        string="Category",
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
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # ── parent links (exactly one should be set) ─────────────────
    till_count_id = fields.Many2one(
        "elks.till.count", ondelete="cascade", index=True,
    )
    safe_count_id = fields.Many2one(
        "elks.safe.count", ondelete="cascade", index=True,
    )

    # ── computes ─────────────────────────────────────────────────
    @api.depends("denomination")
    def _compute_category(self):
        for line in self:
            line.category = DENOM_CATEGORY.get(line.denomination, False)

    @api.depends("denomination")
    def _compute_face_value(self):
        for line in self:
            line.face_value = DENOMINATION_VALUES.get(line.denomination, 0.0)

    @api.depends("face_value", "quantity")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.face_value * (line.quantity or 0)
