from synapse.identifiers import derive_session_key, safe_component


def test_safe_component_normalizes_unsafe_characters() -> None:
    assert safe_component("  hello/world  ") == "hello-world"


def test_derive_session_key_is_stable_and_safe() -> None:
    session_key = derive_session_key("telegram", "chat/123", "user 99")
    assert session_key == derive_session_key("telegram", "chat/123", "user 99")
    assert "/" not in session_key
    assert " " not in session_key
