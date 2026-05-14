# -*- coding: utf-8 -*-
"""Treasury Session — groups all counts and slips for a single night.

At the end of each business night the Secretary performs a full count:
every till gets counted, the safe gets counted, and any change slips
from the shift are attached.  The Treasury Session is the parent record
that ties it all together and provides the summary view.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ElksTreasurySession(models.Model):
    _name = "elks.treasury.session"
    _description = "Treasury Session"
    _order = "session_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    session_date = fields.Date(
        "Session Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
    )
    secretary_id = fields.Many2one(
        "res.users", string="Secretary",
        default=lambda self: self.env.user, tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Open'),
        ('done', 'Closed'),
    ], default='draft', tracking=True, index=True)

    # ── child records ───────────────────────────────────────────
    till_count_ids = fields.One2many(
        "elks.till.count", "session_id", string="Till Counts",
    )
    safe_count_ids = fields.One2many(
        "elks.safe.count", "session_id", string="Safe Counts",
    )
    change_slip_ids = fields.One2many(
        "elks.change.slip", "session_id", string="Change Slips",
    )
    bank_change_ids = fields.One2many(
        "elks.bank.change.request", "session_id",
        string="Bank Change Requests",
    )

    # ── summary totals ──────────────────────────────────────────
    total_tills = fields.Monetary(
        "Total Tills", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_safe = fields.Monetary(
        "Total Safe", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_change_slips = fields.Monetary(
        "Total Change Slips", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    grand_total = fields.Monetary(
        "Grand Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    till_count = fields.Integer(
        "# Tills", compute="_compute_totals", store=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    note = fields.Text("Notes")

    @api.depends("session_date")
    def _compute_name(self):
        for rec in self:
            if rec.session_date:
                rec.name = f"Treasury Session — {rec.session_date}"
            else:
                rec.name = "New Treasury Session"

    @api.depends(
        "till_count_ids.total",
        "safe_count_ids.total",
        "change_slip_ids.total",
    )
    def _compute_totals(self):
        for rec in self:
            tills = sum(rec.till_count_ids.mapped('total'))
            safe = sum(rec.safe_count_ids.mapped('total'))
            slips = sum(rec.change_slip_ids.mapped('total'))
            rec.total_tills = tills
            rec.total_safe = safe
            rec.total_change_slips = slips
            rec.grand_total = tills + safe
            rec.till_count = len(rec.till_count_ids)

    # ── actions ─────────────────────────────────────────────────
    def action_close(self):
        """Close the session — all child counts must be done."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Session is already closed."))
            open_tills = rec.till_count_ids.filtered(
                lambda t: t.state != 'done'
            )
            if open_tills:
                raise UserError(_(
                    "All till counts must be marked Done before "
                    "closing the session. Open tills: %s",
                    ', '.join(open_tills.mapped('till_name')),
                ))
            open_safe = rec.safe_count_ids.filtered(
                lambda s: s.state != 'done'
            )
            if open_safe:
                raise UserError(_(
                    "All safe counts must be marked Done before "
                    "closing the session."
                ))
            rec.state = 'done'
            rec.message_post(
                body=_(
                    "<strong>Session Closed</strong><br/>"
                    "Tills: $%(tills).2f | Safe: $%(safe).2f | "
                    "Grand Total: $%(grand).2f",
                    tills=rec.total_tills,
                    safe=rec.total_safe,
                    grand=rec.grand_total,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reopen(self):
        for rec in self:
            if rec.state != 'done':
                raise UserError(_("Only closed sessions can be re-opened."))
            rec.state = 'draft'

    def action_add_till(self):
        """Create a new till count linked to this session."""
        self.ensure_one()
        till = self.env['elks.till.count'].create({
            'session_id': self.id,
            'session_date': self.session_date,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'elks.till.count',
            'res_id': till.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_add_safe_count(self):
        """Create a new safe count linked to this session."""
        self.ensure_one()
        safe = self.env['elks.safe.count'].create({
            'session_id': self.id,
            'session_date': self.session_date,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'elks.safe.count',
            'res_id': safe.id,
            'view_mode': 'form',
            'target': 'current',
        }
