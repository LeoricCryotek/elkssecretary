# -*- coding: utf-8 -*-
"""Generate Lodge Meeting Agenda — pick a date, click Generate.

[Human]
    Secretary opens this wizard from the Meeting Agenda menu, picks
    the upcoming meeting date, chooses whether to reuse an existing
    agenda or start fresh, clicks Generate.  The system creates the
    agenda record with officers, committees, propositions from the
    membership pipeline, upcoming events, and the current meeting-
    money totals all pre-filled.  You land on the agenda form to
    review and edit.

[AI]
    • TransientModel — one-shot use.
    • action_generate(): searches for an existing meeting on the
      chosen date; if found and reuse=True, opens it; otherwise
      creates a new meeting and calls _populate_from_system() on it.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ElksLodgeMeetingGenerate(models.TransientModel):
    _name = "elks.lodge.meeting.generate"
    _description = "Generate Lodge Meeting Agenda Wizard"

    meeting_date = fields.Date(
        "Meeting Date", required=True,
        default=fields.Date.context_today,
        help="Date of the meeting the agenda is for.",
    )
    reuse_existing = fields.Boolean(
        "Reuse existing draft if one exists", default=True,
        help="If an agenda already exists for this date and is still "
             "draft, open it instead of creating a duplicate.",
    )

    def action_generate(self):
        self.ensure_one()
        Meeting = self.env['elks.lodge.meeting']
        existing = Meeting.search(
            [('meeting_date', '=', self.meeting_date)],
            limit=1,
        )
        if existing and self.reuse_existing:
            meeting = existing
        elif existing:
            raise UserError(_(
                "An agenda already exists for %s.  Tick "
                "\"Reuse existing draft\" to open it, or delete it "
                "first to create a fresh one.") % self.meeting_date)
        else:
            meeting = Meeting.create({
                'meeting_date': self.meeting_date,
            })
            meeting._populate_from_system()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Lodge Meeting — %s") % self.meeting_date,
            'res_model': 'elks.lodge.meeting',
            'res_id': meeting.id,
            'view_mode': 'form',
            'target': 'current',
        }
