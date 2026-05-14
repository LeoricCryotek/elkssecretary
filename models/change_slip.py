# -*- coding: utf-8 -*-
"""Change Slip — record of change made from the safe for a till.

When a bartender or server needs change during a shift, the Secretary
opens the safe, counts out the requested denominations, and records
the exchange on a change slip.  The slip tracks what went out and
ties back to the treasury session.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .denomination_line import DENOMINATION_SELECTION, DENOMINATION_VALUES


class ElksChangeSlip(models.Model):
    _name = "elks.change.slip"
    _description = "Change Slip"
    _order = "slip_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    slip_date = fields.Date(
        "Date", required=True,
        default=fields.Date.context_today, index=True,
    )
    session_id = fields.Many2one(
        "elks.treasury.session", string="Count Session",
        ondelete="set null", index=True,
    )
    requested_by = fields.Many2one(
        "res.users", string="Requested By", tracking=True,
    )
    approved_by = fields.Many2one(
        "res.users", string="Approved By",
        default=lambda self: self.env.user, tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    # ── denomination lines ──────────────────────────────────────
    denomination_ids = fields.One2many(
        "elks.denomination.line", "change_slip_id",
        string="Denominations Given",
    )

    total = fields.Monetary(
        "Total", compute="_compute_total", store=True,
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )
    note = fields.Text("Notes")

    @api.depends("slip_date")
    def _compute_name(self):
        for rec in self:
            if rec.slip_date:
                rec.name = f"Change Slip — {rec.slip_date}"
            else:
                rec.name = "New Change Slip"

    @api.depends("denomination_ids.subtotal")
    def _compute_total(self):
        for rec in self:
            rec.total = sum(rec.denomination_ids.mapped('subtotal'))

    def action_done(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft slips can be confirmed."))
            rec.state = 'done'

    def action_cancel(self):
        for rec in self:
            if rec.state == 'done':
                raise UserError(_("Cannot cancel a confirmed slip."))
            rec.state = 'cancelled'

    def action_reset_draft(self):
        for rec in self:
            if rec.state != 'cancelled':
                raise UserError(_("Only cancelled slips can be reset."))
            rec.state = 'draft'
