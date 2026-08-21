from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

import pytest

from akuma_daemon.telegram_agent import AgentConfigurationError, BotPaths, ConversationStore, TelegramAgentRuntime
from akuma_daemon.telegram_manager import BotConfig, TelegramManager
from akuma_daemon.telegram_speech import VoiceTranscription, VoiceTranscriptionConfigurationError, VoiceTranscriptionError, voice_transcription_settings


class FakeBotRuntime:
    def __init__(self, path: Path, data: dict):
        self.config = BotConfig(data, path)
        self.sent: list[tuple[str, str, bool]] = []
        self.deleted: list[tuple[str, int]] = []
        self.typing_calls: list[str] = []
        self.reply_markups: list[dict | None] = []
        self.callback_answers: list[tuple[str, str, bool]] = []

    def send(self, chat_id, text, thread_id=None, reply_markup=None, protect_content=False):
        self.sent.append((str(chat_id), text, protect_content))
        self.reply_markups.append(reply_markup)
        return {"message_id": len(self.sent)}

    def delete(self, chat_id, message_id):
        self.deleted.append((str(chat_id), message_id))
        return True

    def typing(self, chat_id):
        self.typing_calls.append(str(chat_id))
        return True

    def answer_callback(self, callback_id, text="", show_alert=False):
        self.callback_answers.append((callback_id, text, show_alert))
        return True


def make_agent(tmp_path: Path, *, context: str = "subbot") -> tuple[TelegramAgentRuntime, FakeBotRuntime]:
    bot_root = tmp_path / "configs" / "telegram" / "bots" / "one"
    work = bot_root / "codexwork"
    home = bot_root / "codexhome"
    vault = bot_root / "vault"
    work.mkdir(parents=True)
    home.mkdir()
    vault.mkdir()
    (work / "AGENTS.md").write_text("# One\n", encoding="utf-8")
    (home / "config.toml").write_text("project_root_markers = []\n", encoding="utf-8")
    database = tmp_path / "one.kdbx"
    database.write_bytes(b"test")
    (vault / "keepass.ini").write_text(
        f"[keepass]\ncli_path = {sys.executable}\ndatabase_path = {database}\ntimeout_seconds = 30\n",
        encoding="utf-8",
    )
    data = {
        "id": "one",
        "agent": {
            "enabled": True,
            "context": context,
            "codex_executable": sys.executable,
            "max_parallel_conversations": 2,
            "filesystem": {"read_access": "restricted" if context == "subbot" else "full"},
        },
        "vault": {
            "profile": "vault/keepass.ini",
            "access": "read_only",
            "auth": {"mode": "windows_credential_manager", "target": "test"},
        },
    }
    path = bot_root / "bot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    bot = FakeBotRuntime(path, data)
    return TelegramAgentRuntime(bot, tmp_path), bot


def test_bot_paths_resolve_akuma_and_subbot_defaults(tmp_path):
    sub_path = tmp_path / "configs" / "telegram" / "bots" / "sub" / "bot.json"
    sub = BotPaths.from_config(sub_path, {"id": "sub", "agent": {"context": "subbot"}}, tmp_path)
    assert sub.codex_home == sub_path.parent / "codexhome"
    assert sub.working_directory == sub_path.parent / "codexwork"

    main_path = tmp_path / "configs" / "telegram" / "bots" / "akuma" / "bot.json"
    main = BotPaths.from_config(main_path, {"id": "akuma", "agent": {"context": "akuma"}}, tmp_path)
    assert main.codex_home is None
    assert main.working_directory == tmp_path.resolve()


def test_contacts_and_pairing_state_are_independent_per_bot(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    for bot_id, owner in (("alpha", 101), ("beta", 202)):
        (bots / f"{bot_id}.json").write_text(json.dumps({"id": bot_id, "owners": [{"user_id": owner}]}), encoding="utf-8")
    manager = TelegramManager(root)
    assert manager._is_owner(manager.bots["alpha"], 101)
    assert not manager._is_owner(manager.bots["beta"], 101)
    assert manager._is_owner(manager.bots["beta"], 202)
    manager.pair_request("alpha")
    assert (bots / "alpha" / "state" / "pairing.json").exists()
    assert not (bots / "beta" / "state" / "pairing.json").exists()


def test_legacy_owners_are_atomically_migrated_and_removed(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    config = bots / "main.json"
    config.write_text(json.dumps({"id": "main", "owners": [{"user_id": 7, "username": "owner"}]}), encoding="utf-8")
    manager = TelegramManager(root)
    assert manager._owners(manager.bots["main"])[0]["telegram_user_id"] == 7
    assert "owners" not in json.loads(config.read_text(encoding="utf-8"))
    assert config.with_suffix(".json.owners.bak").exists()


def test_subbot_validation_fails_closed_without_restricted_read_protocol(tmp_path):
    agent, _bot = make_agent(tmp_path)
    agent._supports_restricted_read = lambda: False
    validation = agent.validate()
    assert not validation["ok"]
    assert any("does not support restricted filesystem read roots" in error for error in validation["errors"])
    agent.close()


def test_read_write_vault_adds_only_declared_kdbx_to_writable_roots(tmp_path):
    agent, _bot = make_agent(tmp_path)
    agent.config["vault"]["access"] = "read_write"
    agent._supports_restricted_read = lambda: True
    policy = agent._sandbox_policy()
    database = str(agent._vault_database())
    assert database in policy["writableRoots"]
    assert str(Path(database).parent) not in policy["writableRoots"]
    agent.close()


def test_thread_is_persistent_and_reasoning_summary_is_ephemeral(tmp_path):
    agent, bot = make_agent(tmp_path)

    class Client:
        def __init__(self):
            self.calls = []
            self.last_error = None

        def request(self, method, params, timeout=None):
            self.calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}, "instructionSources": [str(agent.paths.working_directory / "AGENTS.md")]}
            if method == "thread/resume":
                return {"thread": {"id": "thread-1"}, "instructionSources": [str(agent.paths.working_directory / "AGENTS.md")]}
            if method == "turn/start":
                agent._notification("item/completed", {"threadId": "thread-1", "item": {"type": "reasoning", "summary": ["Analisando o pedido"]}})
                agent._notification("item/completed", {"threadId": "thread-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "Resposta final"}})
                agent._notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})
                return {"turn": {"id": "turn-1"}}
            return {}

        def stop(self):
            pass

    agent.client = Client()
    agent.state = "running"
    key = agent.store.key(55)
    first = agent.store.add_inbox(key, 1, {"text": "Olá", "attachments": []})
    rows = agent.store.take_pending(key, 50, 50 * 1024 * 1024)
    assert rows[0]["id"] == first
    agent._run_batch(key, rows)
    agent.store.finish(rows)
    assert agent.store.conversation(key)["codex_thread_id"] == "thread-1"
    assert any(text == "💭 Analisando o pedido" for _chat, text, _protected in bot.sent)
    assert any(text == "Resposta final" for _chat, text, _protected in bot.sent)
    assert bot.deleted == [("55", 1)]

    rows_id = agent.store.add_inbox(key, 2, {"text": "Continue", "attachments": []})
    rows = agent.store.take_pending(key, 50, 50 * 1024 * 1024)
    assert rows[0]["id"] == rows_id
    agent._run_batch(key, rows)
    assert [method for method, _params in agent.client.calls].count("thread/start") == 1
    assert [method for method, _params in agent.client.calls].count("thread/resume") == 1
    agent.close()


def test_thread_contract_combines_gateway_and_bot_instructions(tmp_path):
    agent, _bot = make_agent(tmp_path)
    instructions = agent.paths.bot_root / "developer.md"
    instructions.write_text("Você administra exclusivamente a Laveli.", encoding="utf-8")
    agent.config["agent"]["instructions"] = {
        "gateway_profile": "telegram-v1",
        "developer_file": "developer.md",
    }
    params = agent._thread_parameters()
    assert "Mandatory Telegram gateway instructions: telegram-v1" in params["developerInstructions"]
    assert "Você administra exclusivamente a Laveli." in params["developerInstructions"]
    assert params["dynamicTools"][0]["name"] == "telegram_gateway"
    assert {item["name"] for item in params["dynamicTools"][0]["tools"]} == {
        "send_message", "ask_menu", "list_attachments", "materialize_attachment"
    }
    assert agent._thread_parameters(for_resume=True).get("dynamicTools") is None
    agent.close()


def test_voice_transcription_is_tagged_briefly_and_interpretation_is_in_developer_instructions(tmp_path):
    agent, _bot = make_agent(tmp_path)
    agent.config["voice_transcription"] = {"enabled": True}

    class Transcriber:
        def transcribe(self, path, settings):
            assert path.name == "voice.ogg"
            assert settings.language == "pt-BR"
            return VoiceTranscription("A reunião é com a Lavelinha.", "pt")

    voice = agent.paths.staging_dir / "voice.ogg"
    voice.write_bytes(b"opus")
    rows = [{"payload": {"text": None, "caption": None, "attachments": [{
        "kind": "voice", "path": str(voice), "name": voice.name, "size": 4,
    }]}}]
    agent.transcriber = Transcriber()
    agent._prepare_voice_transcriptions(rows)
    inputs = agent._inputs(rows)
    assert inputs == [{"type": "text", "text": "[Transcrição de mensagem de áudio recebida pelo Telegram]\nA reunião é com a Lavelinha."}]
    developer = agent._developer_instructions()
    assert "names, homophones" in developer
    assert "explicitly tell the user what you understood" in developer
    assert str(voice) not in inputs[0]["text"]
    agent.close()


def test_voice_transcription_failure_keeps_only_an_attachment_marker(tmp_path):
    agent, _bot = make_agent(tmp_path)
    agent.config["voice_transcription"] = {"enabled": True}

    class Transcriber:
        def transcribe(self, path, settings):
            raise VoiceTranscriptionError("empty_transcription")

    voice = agent.paths.staging_dir / "voice.ogg"
    voice.write_bytes(b"opus")
    rows = [{"payload": {"attachments": [{"kind": "voice", "path": str(voice), "name": voice.name, "size": 4}]}}]
    agent.transcriber = Transcriber()
    agent._prepare_voice_transcriptions(rows)
    assert rows[0]["payload"]["attachments"][0]["transcription_error"] == "empty_transcription"
    assert agent._inputs(rows) == [{"type": "text", "text": "[Mensagem de áudio recebida pelo Telegram sem transcrição automática disponível.]"}]
    agent.close()


def test_voice_transcription_requires_loopback_eccovox_endpoint():
    assert voice_transcription_settings({"voice_transcription": {"enabled": True}}).base_url == "http://127.0.0.1:8870"
    with pytest.raises(VoiceTranscriptionConfigurationError):
        voice_transcription_settings({"voice_transcription": {"enabled": True, "base_url": "http://speech.example:8870"}})


def test_developer_file_cannot_escape_bot_directory(tmp_path):
    agent, _bot = make_agent(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    agent.config["agent"]["instructions"] = {"developer_file": str(outside)}
    with pytest.raises(AgentConfigurationError):
        agent._developer_instructions()
    agent.close()


def test_contract_change_deletes_old_thread_and_starts_a_new_one(tmp_path):
    agent, _bot = make_agent(tmp_path)

    class Client:
        last_error = None

        def __init__(self):
            self.calls = []

        def request(self, method, params, timeout=None):
            self.calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "new-thread"}, "instructionSources": [str(agent.paths.working_directory / "AGENTS.md")]}
            if method == "turn/start":
                agent._notification("item/completed", {"threadId": "new-thread", "item": {"type": "agentMessage", "phase": "final_answer", "text": "ok"}})
                agent._notification("turn/completed", {"threadId": "new-thread", "turn": {"status": "completed"}})
                return {"turn": {"id": "turn"}}
            return {}

        def stop(self):
            pass

    agent.client = Client(); agent.state = "running"
    key = agent.store.key(10)
    agent.store.set_thread(key, "old-thread", contract_hash="obsolete")
    row_id = agent.store.add_inbox(key, 1, {"text": "hi", "attachments": []})
    rows = agent.store.take_pending(key, 50, 50 * 1024 * 1024)
    assert rows[0]["id"] == row_id
    agent._run_batch(key, rows)
    assert [method for method, _params in agent.client.calls][:2] == ["thread/delete", "thread/start"]
    assert agent.store.conversation(key)["codex_thread_id"] == "new-thread"
    agent.close()


def test_inline_menu_is_scoped_to_owner_and_returns_selection(tmp_path):
    agent, bot = make_agent(tmp_path)
    agent.contacts.add_owner({"id": 55, "username": "owner", "first_name": "Owner"})
    active = __import__("akuma_daemon.telegram_agent", fromlist=["ActiveTurn"]).ActiveTurn(
        agent.store.key(55), 0, "thread-1", "turn-1", threading.Event()
    )
    result: dict = {}

    def call_tool():
        result.update(agent._tool_ask_menu(active, {
            "question": "Escolha", "options": [{"id": "yes", "label": "Sim"}, {"id": "no", "label": "Não"}],
            "timeout_seconds": 10,
        }))

    worker = threading.Thread(target=call_tool)
    worker.start()
    deadline = time.time() + 2
    while not agent.menus and time.time() < deadline:
        time.sleep(0.01)
    token, session = next(iter(agent.menus.items()))
    assert agent.handle_callback({
        "id": "callback-1", "data": f"ag:{token}:0", "from": {"id": 55},
        "message": {"message_id": session.message_id, "chat": {"id": 55, "type": "private"}},
    })
    worker.join(timeout=2)
    assert result["success"] is True
    assert '"id":"yes"' in result["contentItems"][0]["text"]
    assert bot.callback_answers == [("callback-1", "Selecionado.", False)]
    assert ("55", session.message_id) in bot.deleted
    agent.close()


def test_past_attachment_is_archived_materialized_and_removed_by_new(tmp_path):
    agent, _bot = make_agent(tmp_path)
    key = agent.store.key(66)
    source_dir = agent.paths.staging_dir / "incoming"
    source_dir.mkdir(parents=True)
    source = source_dir / "report.txt"
    source.write_text("laveli", encoding="utf-8")
    payload = {"text": "arquivo", "attachments": [{"kind": "document", "path": str(source), "name": "report.txt", "size": 6}]}
    agent.store.archive_payload_attachments(key, payload, agent.paths.staging_dir, agent.paths.state_dir / "attachments")
    attachment_id = payload["attachments"][0]["attachment_id"]
    active = __import__("akuma_daemon.telegram_agent", fromlist=["ActiveTurn"]).ActiveTurn(
        key, 0, "thread-1", "turn-1", threading.Event()
    )
    listed = agent._server_request("item/tool/call", {
        "namespace": "telegram_gateway", "tool": "list_attachments", "threadId": "thread-1", "arguments": {}
    })
    assert listed["success"] is False  # no active turn can use the conversation capability
    agent.active["thread-1"] = active
    materialized = agent._tool_materialize_attachment(active, {"attachment_id": attachment_id})
    materialized_data = json.loads(materialized["contentItems"][0]["text"])
    assert Path(materialized_data["path"]).read_text(encoding="utf-8") == "laveli"
    archive = Path(agent.store.attachment(key, attachment_id)["archive_path"])
    agent.reset_conversation(66)
    assert agent.store.attachment(key, attachment_id) is None
    assert not archive.exists()
    agent.close()


def test_attachment_batches_respect_combined_size(tmp_path):
    agent, _bot = make_agent(tmp_path)
    key = agent.store.key(88)
    agent.store.add_inbox(key, 1, {"text": "one", "attachments": [{"size": 20, "path": "missing-one"}]})
    agent.store.add_inbox(key, 2, {"text": "two", "attachments": [{"size": 20, "path": "missing-two"}]})
    agent.store.add_inbox(key, 3, {"text": "three", "attachments": [{"size": 20, "path": "missing-three"}]})
    first = agent.store.take_pending(key, 50, 50)
    assert [row["payload"]["text"] for row in first] == ["one", "two"]
    agent.store.finish(first)
    second = agent.store.take_pending(key, 50, 50)
    assert [row["payload"]["text"] for row in second] == ["three"]
    agent.close()


def test_pending_inbox_survives_store_restart_but_running_turn_does_not_repeat(tmp_path):
    path = tmp_path / "state" / "gateway.sqlite3"
    store = ConversationStore(path)
    pending_key = store.key(1)
    running_key = store.key(2)
    store.add_inbox(pending_key, 1, {"text": "pending", "attachments": []})
    store.add_inbox(running_key, 2, {"text": "running", "attachments": []})
    store.take_pending(running_key, 50, 50 * 1024 * 1024)
    store.close()

    reopened = ConversationStore(path)
    assert reopened.pending_keys() == [pending_key]
    assert not reopened.has_pending(running_key)
    reopened.close()


def test_external_instruction_source_is_rejected_for_subbot(tmp_path):
    agent, _bot = make_agent(tmp_path)
    with pytest.raises(AgentConfigurationError):
        agent._validate_instruction_sources({"instructionSources": [str(tmp_path / "AGENTS.md")]})
    agent.close()


def test_new_interrupts_deletes_and_unlinks_thread(tmp_path):
    agent, _bot = make_agent(tmp_path)
    calls = []

    class Client:
        last_error = None

        def request(self, method, params, timeout=None):
            calls.append((method, params))
            return {}

        def stop(self):
            pass

    agent.client = Client()
    key = agent.store.key(77)
    agent.store.set_thread(key, "old-thread")
    agent.active["old-thread"] = __import__("akuma_daemon.telegram_agent", fromlist=["ActiveTurn"]).ActiveTurn(
        key, 0, "old-thread", "old-turn", threading.Event()
    )
    agent.reset_conversation(77)
    assert [method for method, _params in calls] == ["turn/interrupt", "thread/delete"]
    assert agent.store.conversation(key)["codex_thread_id"] is None
    assert agent.store.conversation(key)["generation"] == 1
    agent.close()
