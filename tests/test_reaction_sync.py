from app.reaction_sync import ReactionSyncDeduper, ReactionSyncEvent


def test_reaction_event_normalizes_and_serializes():
    event = ReactionSyncEvent(
        origin_platform="telegram",
        target_chat_id="-100",
        target_message_id="7001",
        reaction_type="EMOJI",
        reaction_value="👍",
        action="ADD",
        actor_key="user:5",
    )

    restored = ReactionSyncEvent.from_dict(event.to_dict())

    assert restored == event
    assert event.reaction_type == "emoji"
    assert event.action == "add"


def test_reaction_deduper_filters_recent_duplicate():
    deduper = ReactionSyncDeduper(ttl_seconds=60)
    event = ReactionSyncEvent(
        origin_platform="max",
        target_chat_id="42",
        target_message_id="max-1",
        reaction_type="emoji",
        reaction_value="🔥",
        action="replace",
        actor_key="user:7",
    )

    assert deduper.check_and_remember(event) is True
    assert deduper.check_and_remember(event) is False
