# -*- coding: utf-8 -*-
"""Bank Change Request — Secretary sends cash to Treasurer for change.

When the house bank is heavy on certain denominations (e.g. $20s, $50s)
and short on others ($1s, $5s), the Secretary fills out a Bank Change
Request specifying what they're sending and what they want back.  The
slip goes in a bank bag to the Treasurer.  Both officers must sign off
on the exchange — the Secretary when sending, the Treasurer when
returning the requested denominations.

The total sent must equal the total requested (equal exchange).
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .denomination_line import DENOMINATION_SELECTION, DENOMINATION_VALUES


class ElksBankChangeRequest(models.Model):
    """Request to exchange denominations through the Treasurer."""

    _name = "elks.bank.change.request"
    _description = "Bank Change Request"
    _order = "request_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    request_date = fields.Date(
        "Request Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent to Treasurer'),
        ('returned', 'Change Returned'),
        ('done', 'Confirmed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True,
       help="Draft: preparing the slip.\n"
            "Sent: bag handed to Treasurer, Secretary signed.\n"
            "Returned: Treasurer brought back change, Treasurer signed.\n"
            "Confirmed: Secretary verified the returned change, both signed.",
    )

    # ── denomination lines ───────────────────────────────────────
    sending_ids = fields.One2many(
        "elks.bank.change.line", "request_id",
        string="Sending (Cash Out)",
        domain=[('line_type', '=', 'sending')],
    )
    requesting_ids = fields.One2many(
        "elks.bank.change.line", "request_id",
        string="Requesting Back",
        domain=[('line_type', '=', 'requesting')],
    )

    # ── totals ───────────────────────────────────────────────────
    total_sending = fields.Monetary(
        "Total Sending", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_requesting = fields.Monetary(
        "Total Requesting", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    is_balanced = fields.Boolean(
        "Balanced", compute="_compute_totals", store=True,
        help="True when total sending equals total requesting.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # ── sign-offs ────────────────────────────────────────────────
    secretary_id = fields.Many2one(
        "res.users", string="Secretary",
        default=lambda self: self.env.user, tracking=True,
    )
    secretary_signed = fields.Boolean("Secretary Signed", tracking=True)
    secretary_sign_date = fields.Datetime("Secretary Signed At", readonly=True)

    treasurer_id = fields.Many2one(
        "res.users", string="Treasurer", tracking=True,
    )
    treasurer_signed = fields.Boolean("Treasurer Signed", tracking=True)
    treasurer_sign_date = fields.Datetime("Treasurer Signed At", readonly=True)

    note = fields.Text("Notes")

    # ── optional session link ────────────────────────────────────
    session_id = fields.Many2one(
        "elks.treasury.session", string="Count Session",
        ondelete="set null", index=True,
    )

    # ── computes ─────────────────────────────────────────────────
    @api.depends("request_date")
    def _compute_name(self):
        for rec in self:
            if rec.request_date:
                rec.name = f"Change Request — {rec.request_date}"
            else:
                rec.name = "New Change Request"

    @api.depends(
        "sending_ids.subtotal",
        "requesting_ids.subtotal",
    )
    def _compute_totals(self):
        for rec in self:
            # Must read all lines and filter by type since domain on
            # One2many is display-only
            all_lines = self.env['elks.bank.change.line'].search([
                ('request_id', '=', rec.id),
            ])
            sending = sum(
                l.subtotal for l in all_lines if l.line_type == 'sending'
            )
            requesting = sum(
                l.subtotal for l in all_lines if l.line_type == 'requesting'
            )
            rec.total_sending = sending
            rec.total_requesting = requesting
            rec.is_balanced = abs(sending - requesting) < 0.01

    # ── actions ──────────────────────────────────────────────────
    def action_populate_sending(self):
        """Pre-fill all denomination lines for the sending side."""
        self.ensure_one()
        existing = set(
            self.env['elks.bank.change.line'].search([
                ('request_id', '=', self.id),
                ('line_type', '=', 'sending'),
            ]).mapped('denomination')
        )
        seq = 10
        lines = []
        for key, label in DENOMINATION_SELECTION:
            if key not in existing:
                lines.append(self.env['elks.bank.change.line'].create({
                    'request_id': self.id,
                    'line_type': 'sending',
                    'denomination': key,
                    'quantity': 0,
                    'sequence': seq,
                }))
            seq += 10

    def action_populate_requesting(self):
        """Pre-fill all denomination lines for the requesting side."""
        self.ensure_one()
        existing = set(
            self.env['elks.bank.change.line'].search([
                ('request_id', '=', self.id),
                ('line_type', '=', 'requesting'),
            ]).mapped('denomination')
        )
        seq = 10
        lines = []
        for key, label in DENOMINATION_SELECTION:
            if key not in existing:
                lines.append(self.env['elks.bank.change.line'].create({
                    'request_id': self.id,
                    'line_type': 'requesting',
                    'denomination': key,
                    'quantity': 0,
                    'sequence': seq,
                }))
            seq += 10

    def action_secretary_sign(self):
        """Secretary signs and sends the bag to Treasurer."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft requests can be sent."))
            if not rec.total_sending:
                raise UserError(_("Add the cash you are sending before signing."))
            if not rec.total_requesting:
                raise UserError(_(
                    "Specify what denominations you want back before signing."
                ))
            if not rec.is_balanced:
                raise UserError(_(
                    "Total sending ($%(send).2f) must equal total requesting "
                    "($%(req).2f). This is an equal exchange.",
                    send=rec.total_sending,
                    req=rec.total_requesting,
                ))
            rec.write({
                'state': 'sent',
                'secretary_signed': True,
                'secretary_sign_date': fields.Datetime.now(),
            })
            rec.message_post(
                body=_(
                    "<strong>Secretary Signed — Bag Sent</strong><br/>"
                    "Sending: $%(send).2f<br/>"
                    "Requesting: $%(req).2f<br/>"
                    "Signed by %(who)s.",
                    send=rec.total_sending,
                    req=rec.total_requesting,
                    who=self.env.user.name,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_treasurer_sign(self):
        """Treasurer signs off — change has been made and returned."""
        for rec in self:
            if rec.state != 'sent':
                raise UserError(_(
                    "The Secretary must sign and send the bag first."
                ))
            rec.write({
                'state': 'returned',
                'treasurer_id': self.env.user.id,
                'treasurer_signed': True,
                'treasurer_sign_date': fields.Datetime.now(),
            })
            rec.message_post(
                body=_(
                    "<strong>Treasurer Signed — Change Returned</strong><br/>"
                    "Change prepared and returned by %(who)s.",
                    who=self.env.user.name,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_confirm(self):
        """Secretary confirms the returned change is correct."""
        for rec in self:
            if rec.state != 'returned':
                raise UserError(_(
                    "The Treasurer must sign off first."
                ))
            rec.state = 'done'
            rec.message_post(
                body=_(
                    "<strong>Change Request Confirmed</strong><br/>"
                    "Secretary verified the returned denominations. "
                    "Exchange complete.",
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel(self):
        """Cancel the request."""
        for rec in self:
            if rec.state == 'done':
                raise UserError(_("Cannot cancel a confirmed exchange."))
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Change Request Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_draft(self):
        """Reset a cancelled request to draft."""
        for rec in self:
            if rec.state != 'cancelled':
                raise UserError(_("Only cancelled requests can be reset."))
            rec.write({
                'state': 'draft',
                'secretary_signed': False,
                'secretary_sign_date': False,
                'treasurer_signed': False,
                'treasurer_sign_date': False,
                'treasurer_id': False,
            })


class ElksBankChangeLine(models.Model):
    """Denomination line for a Bank Change Request.

    Separate model from elks.denomination.line because these lines have
    a line_type (sending vs requesting) and belong to a different parent.
    """
    _name = "elks.bank.change.line"
    _description = "Bank Change Request Line"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    request_id = fields.Many2one(
        "elks.bank.change.request", required=True,
        ondelete="cascade", index=True,
    )
    line_type = fields.Selection([
        ('sending', 'Sending'),
        ('requesting', 'Requesting'),
    ], required=True, index=True,
       help="'Sending' = cash going to the Treasurer. "
            "'Requesting' = denominations wanted back.",
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
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    @api.depends("denomination")
    def _compute_face_value(self):
        for line in self:
            line.face_value = DENOMINATION_VALUES.get(line.denomination, 0.0)

    @api.depends("face_value", "quantity")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.face_value * (line.quantity or 0)
