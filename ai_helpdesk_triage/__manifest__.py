{
    "name": "AI Helpdesk Triage Agent",
    "version": "19.0.1.0.0",
    "category": "Services/Helpdesk",
    "summary": "Human-reviewed Anthropic triage for support tickets",
    "description": """
AI Helpdesk Triage Agent
========================
Adds an agentic first-pass triage workflow for support tickets. The AI can
classify, prioritize, route, draft a reply, explain its reasoning, and record
cost/token telemetry, while every post-triage workflow transition remains a
human action.
""",
    "author": "Mohammad Thabet, Omar Alraas",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ai_triage_actions.xml",
        "views/helpdesk_ticket_views.xml",
        "views/helpdesk_team_views.xml",
        "views/helpdesk_correction_views.xml",
        "views/res_config_settings_views.xml",
        "views/menu_views.xml",
    ],
    "demo": [
        "data/demo_teams.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
