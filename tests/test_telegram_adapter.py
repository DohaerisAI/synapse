from synapse.adapters import TelegramAdapter


def test_telegram_adapter_normalizes_photo_without_caption() -> None:
    adapter = TelegramAdapter(token="token")

    normalized = adapter.normalize_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 22},
                "from": {"id": 44},
                "photo": [
                    {
                        "file_id": "small",
                        "file_unique_id": "u1",
                        "width": 90,
                        "height": 90,
                    },
                    {
                        "file_id": "large",
                        "file_unique_id": "u2",
                        "width": 640,
                        "height": 480,
                        "file_size": 12345,
                    },
                ],
            },
        }
    )

    assert normalized.text == "User uploaded: photo"
    assert normalized.metadata["attachments"][0]["kind"] == "photo"
    assert normalized.metadata["attachments"][0]["file_id"] == "large"


def test_telegram_adapter_normalizes_document_without_caption() -> None:
    adapter = TelegramAdapter(token="token")

    normalized = adapter.normalize_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 22},
                "from": {"id": 44},
                "document": {
                    "file_id": "doc1",
                    "file_unique_id": "du1",
                    "file_name": "report.xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                },
            },
        }
    )

    assert normalized.text == "User uploaded: document report.xlsx"
    assert normalized.metadata["attachments"][0]["file_name"] == "report.xlsx"


def test_telegram_adapter_send_text_uses_html_parse_mode_for_bold() -> None:
    calls = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"ok": True}

    class DummyClient:
        def post(self, url, json=None):  # noqa: A002
            calls["url"] = url
            calls["json"] = json
            return DummyResponse()

    adapter = TelegramAdapter(token="token", client=DummyClient())

    adapter.send_text("22", "**Newcastle vs Man United**")

    assert calls["json"]["parse_mode"] == "HTML"
    assert calls["json"]["text"] == "<b>Newcastle vs Man United</b>"


def test_telegram_adapter_send_text_preserves_code_blocks() -> None:
    calls = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"ok": True}

    class DummyClient:
        def post(self, url, json=None):  # noqa: A002
            calls["json"] = json
            return DummyResponse()

    adapter = TelegramAdapter(token="token", client=DummyClient())

    adapter.send_text("22", "```python\nprint('hi')\n```")

    assert calls["json"]["text"] == "<pre>print(&#x27;hi&#x27;)</pre>"
