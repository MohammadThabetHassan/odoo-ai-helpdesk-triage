def action_ai_resolve(self):
    """Run the agentic resolution loop on triaged tickets."""
    for ticket in self:
        ticket._lock_for_resolve()
        ticket._ensure_can_resolve()
        autonomy_level = ticket._get_autonomy_level()
        if autonomy_level == "off":
            raise UserError(
                _("Autonomous resolution is disabled in AI Helpdesk settings."),
            )
        if autonomy_level == "full":
            ticket._ensure_category_allowed_for_full_autonomy()

        resolve_values = {
            "ai_resolution_in_progress": True,
            "ai_resolution_status": "in_progress",
        }
        if not ticket.ai_resolution_start_at:
            resolve_values["ai_resolution_start_at"] = fields.Datetime.now()
        ticket.with_context(ai_skip_correction_log=True).write(resolve_values)

        max_actions = int(
            ticket._get_float_param(
                "ai_helpdesk_triage.max_actions_per_ticket",
                5,
                minimum=1,
                maximum=20,
            ),
        )
        ...
