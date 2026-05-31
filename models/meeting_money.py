# -*- coding: utf-8 -*-
"""Meeting Money — Fines & Project Dollars collected at lodge meetings.

At each lodge meeting the Trustees pass the cup for Project Dollars and
collect fines.  This model records the totals per meeting and provides
a rolling year-to-date Project Dollars total for the current lodge
year (April 1 – March 31).
"""
from odoo import api, fields, models, _


def _lodge_year_for(date):
    """Return ('YYYY-YYYY', start_date, end_date) for the lodge year
    that contains *date*.  Lodge year runs Apr 1 – Mar 31."""
    if not date:
        return False, False, False
    start_year = date.year if date.month >= 4 else date.year - 1
    start = fields.Date.from_string(f"{start_year}-04-01")
    end = fields.Date.from_string(f"{start_year + 1}-03-31")
    label = f"{start_year}-{start_year + 1}"
    return label, start, end


class ElksMeetingMoney(models.Model):
    _name = "elks.meeting.money"
    _description = "Meeting Money — Fines & Project Dollars"
    _order = "meeting_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        compute="_compute_name", store=True,
    )
    meeting_date = fields.Date(
        "Meeting Date", required=True,
        default=fields.Date.context_today, index=True,
        tracking=True,
    )
    fines_amount = fields.Monetary(
        "Fines Collected", currency_field="currency_id", tracking=True,
        help="Total fines collected at the meeting.",
    )
    project_dollars_amount = fields.Monetary(
        "Project Dollars Collected", currency_field="currency_id",
        tracking=True,
        help="Total Project Dollars collected (Trustees' cup).",
    )
    total_amount = fields.Monetary(
        "Total Collected", compute="_compute_total_amount", store=True,
        currency_field="currency_id",
    )
    collected_by = fields.Char(
        "Collected By(s)", tracking=True,
        help="Name(s) of the Trustee(s) or member(s) who took in "
             "the money.  Separate multiple names with commas.",
    )
    note = fields.Text("Notes")

    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    # ── Lodge-year tracking ───────────────────────────────────────
    lodge_year = fields.Char(
        "Lodge Year", compute="_compute_lodge_year",
        store=True, index=True,
        help="Lodge year (April 1 – March 31) this meeting falls in.",
    )
    project_dollars_ytd = fields.Monetary(
        "Project $ YTD (Lodge Year)",
        compute="_compute_project_dollars_ytd",
        currency_field="currency_id",
        help="Running Project Dollars total for this lodge year "
             "through and including this meeting date.",
    )
    fines_ytd = fields.Monetary(
        "Fines YTD (Lodge Year)",
        compute="_compute_project_dollars_ytd",
        currency_field="currency_id",
    )

    # ── Computes ──────────────────────────────────────────────────
    @api.depends("meeting_date")
    def _compute_name(self):
        for rec in self:
            if rec.meeting_date:
                rec.name = f"Meeting Money — {rec.meeting_date}"
            else:
                rec.name = "New Meeting Money"

    @api.depends("fines_amount", "project_dollars_amount")
    def _compute_total_amount(self):
        for rec in self:
            rec.total_amount = (rec.fines_amount or 0.0) + \
                (rec.project_dollars_amount or 0.0)

    @api.depends("meeting_date")
    def _compute_lodge_year(self):
        for rec in self:
            label, _start, _end = _lodge_year_for(rec.meeting_date)
            rec.lodge_year = label or False

    @api.depends("meeting_date", "project_dollars_amount", "fines_amount",
                 "lodge_year")
    def _compute_project_dollars_ytd(self):
        for rec in self:
            if not rec.meeting_date or not rec.lodge_year:
                rec.project_dollars_ytd = 0.0
                rec.fines_ytd = 0.0
                continue
            domain = [
                ('lodge_year', '=', rec.lodge_year),
                ('meeting_date', '<=', rec.meeting_date),
            ]
            if isinstance(rec.id, int):
                # include other records dated the same day, exclude this one,
                # then add this record's own values at the bottom
                peer_domain = domain + [('id', '!=', rec.id)]
            else:
                peer_domain = domain
            peers = self.search(peer_domain)
            rec.project_dollars_ytd = sum(
                peers.mapped('project_dollars_amount')
            ) + (rec.project_dollars_amount or 0.0)
            rec.fines_ytd = sum(peers.mapped('fines_amount')) + \
                (rec.fines_amount or 0.0)

    # ── Actions ───────────────────────────────────────────────────
    def action_print_project_dollar_slips(self):
        """Print a full sheet of blank Project Dollar slips.

        This action does not require a record - it always prints the
        blank slip template.
        """
        return self.env.ref(
            'elkssecretary.action_report_project_dollar_slip_sheet'
        ).report_action(self)
