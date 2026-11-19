{
    "name": "IC Demo — Real-Time Dashboard",
    "summary": "OWL dashboard with Odoo Bus live updates per company (C4 of ic_demo)",
    "version": "20.0.1.1.0",
    "category": "Tools",
    "license": "LGPL-3",
    "author": "Techvoot",
    "depends": [
        "web",
        "bus",
        "sale_management",
        "account",
    ],
    "data": [
        "security/ir.access.csv",
        "views/dashboard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ic_demo_dashboard/static/src/**/*",
        ],
    },
    "installable": True,
    "application": True,
}
