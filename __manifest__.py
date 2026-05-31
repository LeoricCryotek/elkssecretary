# -*- coding: utf-8 -*-
{
    "name": "Elks Secretary — Daily Report, CLMS Queue & Treasury",
    "version": "19.0.1.16",
    "category": "Productivity",
    "summary": "Daily Secretary dashboard, CLMS processing queue, and "
               "Treasury house cash management (till counts, safe counts, "
               "change slips).",
    "description": """
Elks Secretary Daily Report
============================

Gives the lodge Secretary (and reception staff who don't have CLMS
access) a single daily dashboard showing everything that needs to flow
into CLMS and the day's activity for meeting reporting.

Sections
--------
* **CLMS Work Queue** — dues payments posted here that still need to be
  entered in CLMS.  Click to mark a payment as processed.
* **Today's Activity** — payments, applications, officer changes,
  volunteer links created today.
* **Upcoming Items** — committee reports, scheduled maintenance, members
  whose dues expire soon.
* **Printable Secretary Report** — single-page PDF for monthly Trustee /
  lodge meetings.
""",
    "author": "Danny Santiago",
    "website": "https://dannysantiago.info",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "purchase",
        "hr_attendance",
        "elkscontacts",
        "elksfrs",
        "elkscharity",
        "elkspurchase",
    ],
    "data": [
        "security/elkssecretary_groups.xml",
        "security/ir.model.access.csv",
        "views/secretary_dashboard_views.xml",
        "views/clms_work_queue_views.xml",
        "views/death_clms_queue_views.xml",
        "views/treasury_session_views.xml",
        "views/till_count_views.xml",
        "views/safe_count_views.xml",
        "views/change_slip_views.xml",
        "views/bank_change_request_views.xml",
        "report/daily_secretary_report.xml",
        "report/treasury_reports.xml",
        "report/meeting_money_reports.xml",
        "views/meeting_money_views.xml",
        "views/elkssecretary_menus.xml",
    ],
    "installable": True,
    "application": True,
}
