# -*- coding: utf-8 -*-
"""Change slip model — tracks cash change requests from staff.

Any staff member can request change from the house bank.  The slip is
printable so a manual written log can be kept alongside the system record.
Slips can be entered after the fact from the written log.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ElksChangeSlip(models.Model):
    """Record of a cash change request from the house bank."""

    _name = "elks.change.slip"
    _description = "Change Slip"
    _order = "request_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Slip #", compute="_compute_name", store=True,
    )
    request_date = fields.Datetime(
        "Request Date", required=True,
        default=fields.Datetime.now, index=True, tracking=True,
    )
    request_date_only = fields.Date(
        "Date", compute="_compute_date_only", store=True, index=True,
    )

    # Who requested — free text so non-system staff can be entered
    requested_by = fields.Char(
        "Requested By", required=True,
        help="Name of the person requesting change.",
    )
    requested_by_user_id = fields.Many2one(
        "res.users", string="Requested By (User)",
        help="Optional link to system user, if applicable.",
    )

    # Amount and denominations
    amount = fields.Monetary(
        "Amount", required=True, currency_field="currency_id",
        help="Total dollar amount of change requested.",
    )
    denomination_given = fields.Text(
        "Change Given",
        help="Description of denominations provided — "
             "e.g. '2x $20, 1x $10 roll of quarters'.",
    )
    denomination_received = fields.Text(
        "Cash Received",
        help="Description of what was turned in — "
             "e.g. '1x $50 bill'.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # Approval / state
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    approved_by = fields.Many2one(
        "res.users", string="Processed By", tracking=True,
        help="Person who processed the change from the bank.",
    )

    # Optional link to treasury session
    session_id = fields.Many2one(
        "elks.treasury.session", string="Count Session",
        ondelete="set null", index=True,
        help="Treasury session this slip is associated with, if any.",
    )

    note = fields.Text("Notes")

    # ── computes ─────────────────────────────────────────────────
    @api.depends("request_date")
    def _compute_date_only(self):
        for rec in self:
            rec.request_date_only = rec.request_date.date() if rec.request_date else False

    @api.depends("request_date", "requested_by")
    def _compute_name(self):
        for rec in self:
            date_str = rec.request_date.strftime('%m/%d/%Y %H:%M') if rec.request_date else ''
            who = rec.requested_by or 'New'
            rec.name = f"Change Slip — {who} — {date_str}"

    # ── actions ──────────────────────────────────────────────────
    def action_complete(self):
        """Mark the change as given."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft slips can be completed."))
            rec.write({
                'state': 'done',
                'approved_by': self.env.user.id,
            })
            rec.message_post(
                body=_(
                    "<strong>Change Slip Completed</strong><br/>"
                    "%(who)s received $%(amt).2f in change.<br/>"
                    "Processed by %(by)s.",
                    who=rec.requested_by,
                    amt=rec.amount,
                    by=self.env.user.name,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel(self):
        """Cancel the slip."""
        for rec in self:
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Change Slip Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_draft(self):
        """Reset to draft."""
        for rec in self:
            rec.state = 'draft'
            rec.approved_by = False
