{
    "name": "IC Demo — Consolidated Reporting",
    "summary": "Fast SQL-backed consolidated P&L across 20+ companies (C1 of ic_demo)",
    "version": "20.0.1.0.0",
    "category": "Accounting",
    "license": "LGPL-3",
    "author": "Techvoot",
    "depends": ["account", "ic_demo_dashboard"],
    "data": [
        "security/ir.access.csv",
        "views/consolidation_views.xml",
    ],
    "installable": True,
    "application": False,
}
