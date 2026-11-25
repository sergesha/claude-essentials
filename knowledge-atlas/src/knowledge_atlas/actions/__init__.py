"""Shared handler registry; transport adapters never implement domain behavior."""


def register_handlers(context):
    from .archives import export_archive, restore_archive
    from .atlas import create_atlas
    from .sources import submit_sources, decide_type
    from .decisions import decide_candidate, revise_candidate
    from .packages import configure_packages
    from .feedback import feedback_action
    return {'archive.export':export_archive, 'archive.restore':restore_archive, 'feedback.record': feedback_action, 'knowledge.revise': feedback_action, 'packages.configure': configure_packages, 'candidate.decide': decide_candidate, 'candidate.revise': revise_candidate, 'atlas.create': create_atlas, 'sources.submit': submit_sources, 'type.decide': decide_type, **context.handlers}


def register_recoveries(context):
    from .decisions import reconcile_decision
    return {"candidate.decide": reconcile_decision, "candidate.revise": reconcile_decision, **context.recoveries}
