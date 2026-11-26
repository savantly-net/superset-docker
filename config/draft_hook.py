"""The draft tag (S7.3; FR-7.3, ADR-011, NFR-20; spike T-3).

Each new chart and dashboard gets the custom tag `draft` at once, unless it comes from the brand
repository. An asset from the repository arrives through `franops-bi deploy`, whose import sets
`is_managed_externally` and the YAML's own `tags: [certified]` (S5.5). Every other new asset is
a draft: an HQ chart over MCP, a REST create, or a copy.

superset_config.py installs this module through FLASK_APP_MUTATOR. Superset calls the mutator
in each process that builds the app: the web workers, `superset mcp run`, Celery, and the CLI.

The write follows Superset's own ObjectUpdater.after_insert (superset/tags/models.py): a Session
bound to the flush connection, get_tag(), then a TaggedObject row. Not tested: a rollback of
the asset after the flush.
"""

import logging

logger = logging.getLogger("franops.draft_hook")

DRAFT_TAG = "draft"

_installed = False


def _tag_draft(object_type: str):
    def listener(_mapper, connection, target) -> None:
        if getattr(target, "is_managed_externally", False):
            return
        from sqlalchemy.orm import Session

        from superset.tags.models import get_tag, ObjectType, TaggedObject, TagType

        with Session(bind=connection) as session:
            tag = get_tag(DRAFT_TAG, session, TagType.custom)
            session.add(
                TaggedObject(
                    tag_id=tag.id,
                    object_id=target.id,
                    object_type=ObjectType[object_type],
                )
            )
            session.commit()
        logger.info("draft hook: tagged %s %s draft", object_type, target.id)

    return listener


def install(app) -> None:
    """Listen for new Slice and Dashboard rows. A second call in one process does nothing."""
    global _installed
    if _installed:
        return
    import sqlalchemy as sqla

    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    sqla.event.listen(Slice, "after_insert", _tag_draft("chart"))
    sqla.event.listen(Dashboard, "after_insert", _tag_draft("dashboard"))
    _installed = True
    logger.info("draft hook installed")
