"""Redaction runs on across a tool call or an approval prompt inside one reply.

A tool call or a permission request is a DISPLAY boundary inside one reply, not the
end of it: the model's text before the boundary and its text after it are one run
of characters, so a credential can start in the first and finish in the second.
Each half alone matches no credential pattern, so a redaction window that ends at
the boundary streams and saves both halves raw, and a reader who joins the two rows
(or the two runs of ``chat_chunk`` frames) has the whole secret.

The boundary still splits the reply for display: narration written before a tool
call stays on the row before it, and the reply a linked channel receives is the
text after the last boundary only. So harmless text is pinned as well: every word
lands on the row it was written in, unchanged, while the credential case comes out
as one placeholder.

These drive the real ``_run_chat`` through ``turn_harness.run_turn`` and read what
the turn sent on the WebSocket and what it persisted. The key is the documented
AWS example key, split so no literal sits in the repo.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import APPROVED, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact

#: The AWS documentation's example access key id, split so no literal sits here.
_HEAD, _TAIL = "AKIA", "IOSFODNN7EXAMPLE"
_KEY = _HEAD + _TAIL
_TAG = REDACTED_CREDENTIAL_TAG

_KINDS = ("permission_request", "tool_call")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _boundary(kind: str, n: int) -> AcpEvent:
    """The *n*-th display boundary of a turn: a tool call, or an approval prompt."""
    if kind == "tool_call":
        return AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id=f"tc-{n}", title="Reading a file", tool_kind="read"
        )
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=f"req-{n}",
        tool_call_id=f"tc-{n}",
        title="Reading a file",
        tool_kind="read",
    )


async def _turn(kind: str, *segments: str | tuple[str, ...]) -> TurnRecord:
    """Run one turn whose text is *segments*, a *kind* boundary between each two.

    A segment is one text chunk, or a tuple of the chunks it streams as; an empty
    last segment ends the turn right after its last boundary. Every prompt is
    approved.
    """
    events: list[AcpEvent] = []
    for n, segment in enumerate(segments):
        if n:
            events.append(_boundary(kind, n))
        for chunk in (segment,) if isinstance(segment, str) else segment:
            if chunk:
                events.append(AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk))
    events.append(_DONE)
    answers = {f"req-{n}": APPROVED for n in range(1, len(segments))}
    return await run_turn(TurnScript(events=events, answers=answers))


def _wire(record: TurnRecord) -> str:
    """Every ``chat_chunk`` the turn streamed, joined in order."""
    return "".join(frame.payload["content"] for frame in record.frames("chat_chunk"))


def _saved(record: TurnRecord) -> list[str]:
    """The assistant rows the turn persisted, in order."""
    return [row["content"] for row in record.rows("assistant")]


def _shown(record: TurnRecord) -> list[str]:
    """Each assistant row as an open tab holds it once every frame has landed: the
    row's ``chat_message``, then each content patch addressed to that row."""
    rows: dict[str, str] = {}
    for frame in record.ws_frames:
        payload: Any = frame.payload
        if frame.kind == "chat_message" and payload.get("role") == "assistant":
            rows[payload["meta"]["mid"]] = payload["content"]
        elif frame.kind == "chat_message_update" and payload.get("mid") in rows:
            rows[payload["mid"]] = payload["content"]
    return list(rows.values())


def _sent_text(record: TurnRecord) -> str:
    """Everything the turn put in a content frame: chunks, rows and row patches."""
    parts = []
    for frame in record.ws_frames:
        if frame.kind in ("chat_chunk", "chat_message", "chat_message_update"):
            parts.append(str(frame.payload.get("content", "")))
    return "\n".join(parts)


def _layout(record: TurnRecord) -> list[str]:
    """The window after the turn: assistant rows by content, and each boundary's
    rows (an approval card, the tool row it gates) as one ``|``."""
    out: list[str] = []
    for row in record.window:
        if row["role"] == "assistant":
            out.append(row["content"])
        elif row["role"] in ("tool", "permission") and out[-1:] != ["|"]:
            out.append("|")
    return out


def _chunks_after_boundary(record: TurnRecord) -> str:
    """The ``chat_chunk`` text streamed after the turn's first boundary was shown."""
    frames = list(record.ws_frames)
    first = next(
        i
        for i, frame in enumerate(frames)
        if frame.kind == "tool_call"
        or (frame.kind == "chat_message" and frame.payload.get("role") == "permission")
    )
    return "".join(f.payload["content"] for f in frames[first:] if f.kind == "chat_chunk")


@pytest.fixture
def mirrored(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The final reply the turn hands its linked channel, recorded."""
    replies: list[str] = []

    async def _record(_state: Any, _key: str, text: str, **_kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(chat_runner, "_deliver_cross_surface_reply", _record)
    return replies


@pytest.mark.parametrize("kind", _KINDS)
class TestCredentialSplitAcrossABoundary:
    @pytest.mark.asyncio
    async def test_the_wire_stream_redacts_the_whole_credential(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        assert _wire(record) == f"Before {_TAG} after."
        sent = _sent_text(record)
        assert _TAIL not in sent and _HEAD not in sent, sent

    @pytest.mark.asyncio
    async def test_the_persisted_history_redacts_the_whole_credential(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        # The narration stays on the row before the boundary; the placeholder
        # opens the row after it, where the credential was completed.
        assert _saved(record) == ["Before ", f"{_TAG} after."]
        assert _shown(record) == _saved(record)

    @pytest.mark.asyncio
    async def test_a_credential_split_over_two_boundaries_is_redacted(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", _TAIL[:4], f"{_TAIL[4:]} after.")

        assert "".join(_saved(record)) == f"Before {_TAG} after."
        assert _wire(record) == f"Before {_TAG} after."
        sent = _sent_text(record)
        assert _TAIL[:4] not in sent and _TAIL[4:] not in sent, sent

    @pytest.mark.asyncio
    async def test_a_credential_whole_before_the_boundary_stays_redacted(self, kind: str) -> None:
        """The key is complete before the boundary, and the text after it starts
        with no space, so joined the two would no longer read as a key: the row
        before the boundary keeps the placeholder it saves on its own."""
        record = await _turn(kind, (f"Key {_HEAD}", _TAIL), "Done.")

        assert _saved(record) == [f"Key {_TAG}", "Done."]
        assert _KEY not in _sent_text(record)

    @pytest.mark.asyncio
    async def test_the_linked_channel_reply_sees_the_credential_whole(
        self, kind: str, mirrored: list[str]
    ) -> None:
        await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        [reply] = mirrored
        assert "Before" not in reply
        assert redact(reply) == f"{_TAG} after."


def test_a_held_run_goes_back_only_on_the_variant_it_was_cut_from() -> None:
    """A run goes back on the row it was cut from, in the content the row holds
    then, and on the variant shown at the cut; a row showing another variant
    since is left as it is."""
    frames: list[tuple[str, dict[str, Any]]] = []
    state = SimpleNamespace(broadcast_ws=lambda kind, payload: frames.append((kind, payload)))
    slot = SimpleNamespace(key="chat-1", workspace=None, _dirty=False)
    row: dict[str, Any] = {
        "role": "assistant",
        "content": "Let me ",
        "ts": "t1",
        "meta": {"mid": "m1"},
        "variants": [{"content": "An earlier reply"}, {"content": "Let me "}],
        "variant_idx": 1,
    }

    chat_runner._give_back_run(state, slot, row, "look.", 1)

    assert row["content"] == "Let me look." and row["variants"][1]["content"] == "Let me look."
    assert slot._dirty is True
    assert frames == [
        (
            "chat_message_update",
            {"slot": "chat-1", "ts": "t1", "mid": "m1", "content": "Let me look."},
        )
    ]
    row.update(content="An earlier reply", variant_idx=0)
    chat_runner._give_back_run(state, slot, row, " again", 1)
    assert row["content"] == "An earlier reply" and len(frames) == 1


@pytest.mark.parametrize("kind", _KINDS)
class TestHarmlessTextAcrossABoundary:
    @pytest.mark.asyncio
    async def test_a_word_split_by_the_boundary_stays_where_it_was_written(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me check the config", "uration file now.")

        assert _saved(record) == ["Let me check the config", "uration file now."]
        assert _shown(record) == _saved(record)
        assert _layout(record) == ["Let me check the config", "|", "uration file now."]
        # The bubble after the boundary streams only its own text.
        assert _chunks_after_boundary(record) == "uration file now."
        assert mirrored == ["uration file now."]

    @pytest.mark.asyncio
    async def test_narration_ending_a_sentence_stays_on_its_row(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me check the config.", "The config says hi.")

        assert _saved(record) == ["Let me check the config.", "The config says hi."]
        assert _shown(record) == _saved(record)
        assert _chunks_after_boundary(record) == "The config says hi."
        assert mirrored == ["The config says hi."]

    @pytest.mark.asyncio
    async def test_a_turn_ending_at_the_boundary_keeps_its_last_word(self, kind: str) -> None:
        record = await _turn(kind, "Let me check the config.", "")

        assert _saved(record) == ["Let me check the config."]
        assert _shown(record) == _saved(record)

    @pytest.mark.asyncio
    async def test_a_word_alone_between_two_boundaries_stays_between_them(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me look.", "OK.", "Found it.")

        assert _saved(record) == ["Let me look.", "OK.", "Found it."]
        assert _shown(record) == _saved(record)
        assert _layout(record) == ["Let me look.", "|", "OK.", "|", "Found it."]
        assert mirrored == ["Found it."]
