# -*- coding: utf-8 -*-
"""Till Count — count cash in a single register/till.

The Secretary counts each till at the end of the night.  Each till has
denomination lines for bills, rolled coins, and loose coins.  The count
can be part of a Treasury Session (which groups all counts for one date).
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .denomination_line import (
    DENOMINATION_SELECTION,
    DENOMINATION_VALUES,
    CATEGORY_SELECTION,
)


class ElksTillCount(models.Model):
    _name = "elks.till.count"
    _description = "Till Count"
    _order = "session_date desc, id desc"
    _inherit = ["mail.thread"]

    till_name = fields.Char("Till Name", required=True, tracking=True)

    session_id = fields.Many2one(
        "elks.treasury.session", string="Count Session",
        ondelete="set null", index=True,
    )
    session_date = fields.Date(
        "Date", required=True,
        default=fields.Date.context_today, index=True,
    )
    counted_by = fields.Many2one(
        "res.users", string="Counted By",
        default=lambda self: self.env.user, tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Counting'),
        ('done', 'Done'),
    ], default='draft', tracking=True, index=True)

    # ── denomination lines ──────────────────────────────────────
    denomination_ids = fields.One2many(
        "elks.denomination.line", "till_count_id",
        string="Denominations",
    )

    # ── totals ──────────────────────────────────────────────────
    total_bills = fields.Monetary(
        "Total Bills", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_rolled = fields.Monetary(
        "Total Rolled", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_loose = fields.Monetary(
        "Total Loose", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total = fields.Monetary(
        "Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    note = fields.Text("Notes")

    @api.depends("denomination_ids.subtotal", "denomination_ids.category")
    def _compute_totals(self):
        for rec in self:
            bills = rolled = loose = 0.0
            for line in rec.denomination_ids:
                if line.category == 'bill':
                    bills += line.subtotal
                elif line.category == 'rolled':
                    rolled += line.subtotal
                else:
                    loose += line.subtotal
            rec.total_bills = bills
            rec.total_rolled = rolled
            rec.total_loose = loose
            rec.total = bills + rolled + loose

    # ── actions ─────────────────────────────────────────────────
    def action_populate_denominations(self):
        """Pre-fill all denomination lines with zero quantity."""
        self.ensure_one()
        existing = set(
            self.denomination_ids.mapped(
                lambda l: (l.denomination, l.category)
            )
        )
        seq = 10
        vals_list = []
        # Bills
        for key, label in DENOMINATION_SELECTION:
            if DENOMINATION_VALUES.get(key, 0) >= 1.0:
                if (key, 'bill') not in existing:
                    vals_list.append({
                        'till_count_id': self.id,
                        'category': 'bill',
                        'denomination': key,
                        'quantity': 0,
                        'sequence': seq,
                    })
                seq += 10
        # Rolled coins
        for key, label in DENOMINATION_SELECTION:
            if DENOMINATION_VALUES.get(key, 0) < 1.0:
                if (key, 'rolled') not in existing:
                    vals_list.append({
                        'till_count_id': self.id,
                        'category': 'rolled',
                        'denomination': key,
                        'quantity': 0,
                        'sequence': seq,
                    })
                seq += 10
        # Loose coins
        for key, label in DENOMINATION_SELECTION:
            if DENOMINATION_VALUES.get(key, 0) < 1.0:
                if (key, 'loose') not in existing:
                    vals_list.append({
                        'till_count_id': self.id,
                        'category': 'loose',
                        'denomination': key,
                        'quantity': 0,
                        'sequence': seq,
                    })
                seq += 10
        if vals_list:
            self.env['elks.denomination.line'].create(vals_list)

    def action_done(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft counts can be marked done."))
            rec.state = 'done'

    def action_reset_draft(self):
        for rec in self:
            if rec.state != 'done':
                raise UserError(_("Only completed counts can be re-opened."))
            rec.state = 'draft'
