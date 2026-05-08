# -*- coding: utf-8 -*-
"""Till count model — denomination-level cash count for a single till.

Each till count belongs to a Treasury Session and contains denomination
lines for bills, rolled coins, and loose coins.  The total is computed
from the lines.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .denomination_line import (
    DENOMINATION_SELECTION,
    DENOMINATION_VALUES,
    DENOM_CATEGORY,
)


class ElksTillCount(models.Model):
    """Cash count for a single register / till."""

    _name = "elks.till.count"
    _description = "Till Count"
    _order = "session_id desc, till_name"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    session_id = fields.Many2one(
        "elks.treasury.session", string="Count Session",
        required=True, ondelete="cascade", index=True,
    )
    session_date = fields.Date(
        related="session_id.session_date", store=True, string="Date",
    )
    till_name = fields.Char(
        "Till / Register", required=True, default="Till 1",
        help="Label for this register — e.g. 'Bar Till', 'Till 2'.",
    )
    state = fields.Selection([
        ('draft', 'Counting'),
        ('done', 'Done'),
    ], default='draft', tracking=True)

    # ── denomination lines ───────────────────────────────────────
    denomination_ids = fields.One2many(
        "elks.denomination.line", "till_count_id",
        string="Denominations",
    )

    # ── totals ───────────────────────────────────────────────────
    total_bills = fields.Monetary(
        "Bills Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_rolled = fields.Monetary(
        "Rolled Coins Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_loose = fields.Monetary(
        "Loose Coins Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total = fields.Monetary(
        "Grand Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    counted_by = fields.Many2one(
        "res.users", string="Counted By",
        default=lambda self: self.env.user,
    )
    note = fields.Text("Notes")

    # ── computes ─────────────────────────────────────────────────
    @api.depends("till_name", "session_id.session_date")
    def _compute_name(self):
        for rec in self:
            date = rec.session_id.session_date or ''
            rec.name = f"{rec.till_name} — {date}" if date else rec.till_name

    @api.depends(
        "denomination_ids.subtotal",
        "denomination_ids.category",
    )
    def _compute_totals(self):
        for rec in self:
            bills = rolled = loose = 0.0
            for line in rec.denomination_ids:
                if line.category == 'bills':
                    bills += line.subtotal
                elif line.category == 'rolled':
                    rolled += line.subtotal
                elif line.category == 'loose':
                    loose += line.subtotal
            rec.total_bills = bills
            rec.total_rolled = rolled
            rec.total_loose = loose
            rec.total = bills + rolled + loose

    # ── actions ──────────────────────────────────────────────────
    def action_populate_denominations(self):
        """Pre-fill all denomination lines with qty = 0 for easy entry."""
        self.ensure_one()
        existing = {l.denomination for l in self.denomination_ids}
        seq = 10
        lines = []
        for key, label in DENOMINATION_SELECTION:
            if key not in existing:
                lines.append((0, 0, {
                    'denomination': key,
                    'quantity': 0,
                    'sequence': seq,
                }))
            seq += 10
        if lines:
            self.denomination_ids = lines

    def action_done(self):
        """Mark this till count as complete."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only counts in 'Counting' state can be finalized."))
            rec.state = 'done'
            rec.message_post(
                body=_(
                    "<strong>Till Count Complete</strong><br/>"
                    "%(till)s — Total: $%(total).2f",
                    till=rec.till_name,
                    total=rec.total,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_draft(self):
        """Re-open a completed count for corrections."""
        for rec in self:
            rec.state = 'draft'
