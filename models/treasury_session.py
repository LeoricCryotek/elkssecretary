# -*- coding: utf-8 -*-
"""Treasury Session — groups till counts and a safe count for one shift.

A Treasury Session is the parent container for end-of-shift cash
counting.  It holds one or more Till Counts (variable number depending
on how many registers were open) plus a single Safe / Bank Count.
The session computes a grand total across all counts and tracks the
expected starting bank amount for variance detection.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ElksTreasurySession(models.Model):
    """Parent container for a shift / event cash count."""

    _name = "elks.treasury.session"
    _description = "Treasury Count Session"
    _order = "session_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    session_date = fields.Date(
        "Session Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
    )
    session_type = fields.Selection([
        ('end_of_day', 'End of Day'),
        ('shift_change', 'Shift Change'),
        ('event', 'Event'),
        ('adhoc', 'Ad-Hoc'),
    ], default='end_of_day', required=True, tracking=True,
       string="Session Type",
    )
    state = fields.Selection([
        ('draft', 'In Progress'),
        ('done', 'Finalized'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    # ── child counts ─────────────────────────────────────────────
    till_count_ids = fields.One2many(
        "elks.till.count", "session_id", string="Till Counts",
    )
    safe_count_ids = fields.One2many(
        "elks.safe.count", "session_id", string="Safe / Bank Counts",
    )
    change_slip_ids = fields.One2many(
        "elks.change.slip", "session_id", string="Change Slips",
    )

    # ── expected bank amount ─────────────────────────────────────
    expected_bank = fields.Monetary(
        "Expected Bank Amount", currency_field="currency_id",
        help="The expected starting amount in the safe/bank. "
             "Used to calculate variance against the actual safe count.",
    )

    # ── totals ───────────────────────────────────────────────────
    till_count = fields.Integer(
        "# Tills", compute="_compute_totals", store=True,
    )
    total_tills = fields.Monetary(
        "All Tills Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_safe = fields.Monetary(
        "Safe Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_change_slips = fields.Monetary(
        "Change Slips Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    grand_total = fields.Monetary(
        "Grand Total", compute="_compute_totals", store=True,
        currency_field="currency_id",
        help="Sum of all tills + safe.",
    )
    safe_variance = fields.Monetary(
        "Safe Variance", compute="_compute_totals", store=True,
        currency_field="currency_id",
        help="Safe total minus expected bank. "
             "Positive = overage, negative = shortage.",
    )

    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # ── people ───────────────────────────────────────────────────
    counted_by = fields.Many2one(
        "res.users", string="Counted By",
        default=lambda self: self.env.user, tracking=True,
    )
    witnessed_by = fields.Many2one(
        "res.users", string="Witnessed By", tracking=True,
    )

    note = fields.Text("Notes")

    # ── computes ─────────────────────────────────────────────────
    @api.depends("session_date", "session_type")
    def _compute_name(self):
        type_labels = dict(self._fields['session_type'].selection)
        for rec in self:
            label = type_labels.get(rec.session_type, 'Count')
            if rec.session_date:
                rec.name = f"Treasury — {label} — {rec.session_date}"
            else:
                rec.name = f"Treasury — {label}"

    @api.depends(
        "till_count_ids.total",
        "safe_count_ids.total",
        "change_slip_ids.amount",
        "change_slip_ids.state",
        "expected_bank",
    )
    def _compute_totals(self):
        for rec in self:
            rec.till_count = len(rec.till_count_ids)
            rec.total_tills = sum(rec.till_count_ids.mapped('total'))
            rec.total_safe = sum(rec.safe_count_ids.mapped('total'))
            # Only completed change slips
            done_slips = rec.change_slip_ids.filtered(
                lambda s: s.state == 'done'
            )
            rec.total_change_slips = sum(done_slips.mapped('amount'))
            rec.grand_total = rec.total_tills + rec.total_safe
            rec.safe_variance = rec.total_safe - (rec.expected_bank or 0)

    # ── actions ──────────────────────────────────────────────────
    def action_add_till(self):
        """Quick-add a new till count to this session."""
        self.ensure_one()
        till_num = len(self.till_count_ids) + 1
        till = self.env['elks.till.count'].create({
            'session_id': self.id,
            'till_name': f"Till {till_num}",
        })
        till.action_populate_denominations()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Till Count'),
            'res_model': 'elks.till.count',
            'res_id': till.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_add_safe(self):
        """Quick-add the safe count to this session."""
        self.ensure_one()
        if self.safe_count_ids:
            # Open existing
            return {
                'type': 'ir.actions.act_window',
                'name': _('Safe Count'),
                'res_model': 'elks.safe.count',
                'res_id': self.safe_count_ids[0].id,
                'view_mode': 'form',
                'target': 'current',
            }
        safe = self.env['elks.safe.count'].create({
            'session_id': self.id,
        })
        safe.action_populate_denominations()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Safe Count'),
            'res_model': 'elks.safe.count',
            'res_id': safe.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_finalize(self):
        """Finalize the session — all child counts must be done."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only in-progress sessions can be finalized."))

            incomplete_tills = rec.till_count_ids.filtered(
                lambda t: t.state != 'done'
            )
            if incomplete_tills:
                names = ', '.join(incomplete_tills.mapped('till_name'))
                raise UserError(_(
                    "Complete all till counts before finalizing. "
                    "Still counting: %s", names,
                ))

            incomplete_safe = rec.safe_count_ids.filtered(
                lambda s: s.state != 'done'
            )
            if incomplete_safe:
                raise UserError(_(
                    "Complete the safe count before finalizing the session."
                ))

            rec.state = 'done'
            rec.message_post(
                body=_(
                    "<strong>Treasury Session Finalized</strong><br/>"
                    "%(count)d till(s): $%(tills).2f<br/>"
                    "Safe: $%(safe).2f<br/>"
                    "Grand Total: $%(grand).2f",
                    count=rec.till_count,
                    tills=rec.total_tills,
                    safe=rec.total_safe,
                    grand=rec.grand_total,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel(self):
        """Cancel the session."""
        for rec in self:
            if rec.state == 'done':
                raise UserError(_(
                    "Cannot cancel a finalized session. "
                    "Contact the Secretary."
                ))
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Treasury Session Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_draft(self):
        """Re-open a cancelled session."""
        for rec in self:
            if rec.state != 'cancelled':
                raise UserError(_("Only cancelled sessions can be reset."))
            rec.state = 'draft'

    def action_view_change_slips(self):
        """Smart button: show change slips for this session."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Change Slips'),
            'res_model': 'elks.change.slip',
            'view_mode': 'list,form',
            'domain': [('session_id', '=', self.id)],
            'context': {'default_session_id': self.id},
        }
