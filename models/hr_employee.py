# -*- coding: utf-8 -*-
"""hr.employee → Lodge Area mapping.

[Human]
    Tags each employee with their primary lodge operating area
    (Kitchen, Lounge, etc.).  This is the fallback path the
    timecard pull uses if the Area doesn't have HR Departments
    mapped — useful when staff move between departments but always
    cost the same area.

[AI]
    • Single field added: elks_area_id (Many2one to elks.area).
    • Used by elks.area.pnl.action_pull_labor_from_timecards
      as scoping precedence #2 (after area.department_ids).
    • No-create option on the form view to prevent accidental
      area creation from the employee form.
    • Form view inherited in views/hr_employee_views.xml — adds
      this field after the Department field on the employee form.
"""
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    elks_area_id = fields.Many2one(
        "elks.area", string="Lodge Area",
        help="Primary lodge operating area for this employee.  "
             "Used to scope Area P&L timecard pulls.",
    )
