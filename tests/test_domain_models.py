from discord_cleanup.models.domain import (
    Guild,
    OperationPreview,
    OperationResult,
    ReadStateEntry,
    Relationship,
    RelationshipType,
    User,
)


class TestDomainModels:
    def test_user_model(self):
        user = User(id="123", username="johndoe", discriminator="1234", global_name="John Doe")
        assert user.display_name == "John Doe"
        assert user.tag == "johndoe#1234"

        user_migrated = User(id="123", username="johndoe", discriminator="0")
        assert user_migrated.display_name == "johndoe"
        assert user_migrated.tag == "@johndoe"

        from_dict_user = User.from_dict({"id": 456, "username": "bob", "global_name": "Bob B"})
        assert from_dict_user.id == "456"
        assert from_dict_user.display_name == "Bob B"

    def test_guild_model(self):
        guild = Guild.from_dict({"id": "999", "name": "Python Guild", "owner": True})
        assert guild.id == "999"
        assert guild.name == "Python Guild"
        assert guild.owner is True

    def test_relationship_model(self):
        rel = Relationship.from_dict({
            "id": "111",
            "type": 1,
            "user": {"id": "111", "username": "alice", "global_name": "Alice"},
            "since": "2023-01-01T00:00:00Z",
        })
        assert rel.rel_type == RelationshipType.FRIEND
        assert rel.user.username == "alice"
        assert rel.since == "2023-01-01T00:00:00Z"

    def test_read_state_entry(self):
        entry = ReadStateEntry(channel_id="c_1", message_id="m_1")
        d = entry.to_dict()
        assert d["channel_id"] == "c_1"
        assert d["message_id"] == "m_1"

    def test_operation_summary_and_result(self):
        preview = OperationPreview(action_name="Leave Servers", target_count=3, target_descriptions=["S1", "S2", "S3"])
        assert preview.target_count == 3
        assert len(preview.target_descriptions) == 3

        result = OperationResult(success_count=5, failure_count=2, errors=["Timeout", "Forbidden"])
        assert result.total_processed == 7
        assert len(result.errors) == 2
