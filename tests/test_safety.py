"""SafetyState/Store persistence and SafetyGuard rules, in isolation."""

from __future__ import annotations

import json
import logging
import os

import pytest

import whatsapp_web
from tests.conftest import FakeClock
from whatsapp_web import (
    AccountAtRiskError,
    CLOCK_SKEW_TOLERANCE_S,
    MAX_PROCESSED_OPT_OUTS,
    PersistentSendHistory,
    SafetyConfig,
    SafetyGuard,
    SafetyState,
    SafetyStore,
    SafetyViolationError,
    SECONDS_PER_DAY,
)


def make_guard(tmp_path, clock=None, **options):
    clock = clock or FakeClock()
    store = SafetyStore(tmp_path / "state.json")
    return SafetyGuard(SafetyConfig(**options), store, clock.as_clock()), store, clock


def corrupt_files(tmp_path):
    return list(tmp_path.glob("state.json.corrupt-*"))


class TestState:
    def test_from_dict_sorts_send_times_and_trims_opt_out_history(self):
        raw = {
            "send_times": [30, 10, 20],
            "group_creations": [5, 1],
            "processed_opt_outs": [str(i) for i in range(MAX_PROCESSED_OPT_OUTS + 5)],
        }
        state = SafetyState.from_dict(raw)
        assert list(state.send_times) == [10, 20, 30]
        assert state.group_creations == [1, 5]
        assert len(state.processed_opt_outs) == MAX_PROCESSED_OPT_OUTS
        assert state.processed_opt_outs[-1] == str(MAX_PROCESSED_OPT_OUTS + 4)

    def test_link_identities_merges_everything(self):
        state = SafetyState(
            opted_out={"name:a"},
            known_senders={"name:a"},
            new_recipients=[(1.0, "name:a")],
            broadcasts=[(1.0, "d", "name:a")],
        )
        state.link_identities("phone:1", "name:a")
        assert state.canonical("name:a") == "phone:1"
        assert state.opted_out == {"phone:1"}
        assert state.known_senders == {"phone:1"}
        assert state.new_recipients == [(1.0, "phone:1")]
        assert state.broadcasts == [(1.0, "d", "phone:1")]
        state.link_identities("phone:1", "name:a")  # already linked: no-op
        state.link_identities("phone:1", "name:b")
        state.link_identities("phone:2", "phone:1")  # re-point earlier links
        assert state.canonical("name:a") == state.canonical("name:b") == "phone:2"

    def test_future_entry_helpers(self):
        state = SafetyState(
            send_times=__import__("collections").deque([1.0, 100.0]),
            new_recipients=[(1.0, "a"), (100.0, "b")],
            broadcasts=[(1.0, "d", "a"), (100.0, "d", "b")],
            group_creations=[1.0, 100.0],
        )
        assert state.has_entries_after(50.0)
        assert not state.has_entries_after(100.0)
        state.drop_entries_after(50.0)
        assert list(state.send_times) == [1.0]
        assert state.new_recipients == [(1.0, "a")]
        assert state.broadcasts == [(1.0, "d", "a")]
        assert state.group_creations == [1.0]


class TestStore:
    def test_missing_file_starts_fresh_without_creating_anything(self, tmp_path):
        store = SafetyStore(tmp_path / "none" / "state.json")
        assert store.state == SafetyState()
        assert not (tmp_path / "none").exists()

    @pytest.mark.parametrize(
        "content", ["{not json", "[1, 2]", '{"send_times": ["x"]}', '{"new_recipients": [1]}']
    )
    def test_corrupt_file_is_quarantined_and_breaker_stays_tripped(self, tmp_path, caplog, content):
        path = tmp_path / "state.json"
        path.write_text(content)
        with caplog.at_level(logging.WARNING):
            guard, store, _ = make_guard(tmp_path)
        assert store.state.breaker_reason == "unreadable safety state"
        assert len(corrupt_files(tmp_path)) == 1
        with pytest.raises(AccountAtRiskError, match="unreadable safety state"):
            guard.check_can_send(["a"], None)
        # the tripped breaker is persisted at once, so a restart cannot forget it
        assert json.loads(path.read_text())["breaker_reason"] == "unreadable safety state"
        restarted, _, _ = make_guard(tmp_path)
        with pytest.raises(AccountAtRiskError):
            restarted.check_can_send(["a"], None)

    def test_unreadable_path_also_trips(self, tmp_path):
        (tmp_path / "state.json").mkdir()  # reading a directory raises OSError
        store = SafetyStore(tmp_path / "state.json")
        assert store.state.breaker_reason == "unreadable safety state"

    def test_quarantine_rename_failure_is_tolerated(self, tmp_path, monkeypatch):
        path = tmp_path / "state.json"
        path.write_text("garbage")
        monkeypatch.setattr(type(path), "rename", lambda self, target: (_ for _ in ()).throw(OSError("no")))
        assert SafetyStore(path).state.breaker_reason == "unreadable safety state"

    def test_roundtrip_preserves_everything(self, tmp_path):
        guard, _, clock = make_guard(tmp_path)
        guard.note_incoming("name:a", "m1", "stop")
        guard.record_send(["name:b"], "Hello", creates_group=True)
        guard.send_history.add(clock.now())
        guard.link_identities("phone:1", "name:b")
        guard.record_failure()
        reloaded = SafetyStore(tmp_path / "state.json").state
        assert reloaded.opted_out == {"name:a"}
        assert reloaded.consecutive_failures == 1
        assert list(reloaded.send_times) == [clock.now()]
        assert reloaded.new_recipients == [(clock.now(), "phone:1")]
        assert reloaded.identity_links == {"name:b": "phone:1"}
        assert reloaded.group_creations == [clock.now()]

    def test_state_file_never_contains_message_text(self, tmp_path):
        guard, _, _ = make_guard(tmp_path)
        guard.record_send(["name:b"], "secret text")
        assert "secret" not in (tmp_path / "state.json").read_text()

    def test_write_failure_trips_breaker_in_memory_and_keeps_the_change(self, tmp_path, caplog):
        blocker = tmp_path / "blocker"
        blocker.write_text("file, not dir")
        store = SafetyStore(blocker / "state.json")
        with caplog.at_level(logging.WARNING):
            store.update(lambda state: state.opted_out.add("name:a"))
        assert "Could not access" in caplog.text
        assert store.state.opted_out == {"name:a"}
        assert "could not persist" in store.state.breaker_reason

    def test_refresh_failure_trips_breaker(self, tmp_path, monkeypatch):
        (tmp_path / "state.json").write_text("{}")
        store = SafetyStore(tmp_path / "state.json")
        monkeypatch.setattr(
            SafetyStore, "_reload_from_disk", lambda self: (_ for _ in ()).throw(OSError("disk"))
        )
        store.refresh()
        assert "could not persist" in store.state.breaker_reason

    def test_failed_replace_leaves_no_temp_files(self, tmp_path, monkeypatch):
        store = SafetyStore(tmp_path / "state.json")
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("busy")))
        store.update(lambda state: state.opted_out.add("x"))
        assert list(tmp_path.glob(".safety-*")) == []
        assert store.state.breaker_reason is not None

    def test_two_stores_do_not_lose_each_others_updates(self, tmp_path):
        first = SafetyStore(tmp_path / "state.json")
        second = SafetyStore(tmp_path / "state.json")
        first.update(lambda state: state.opted_out.add("name:a"))
        second.update(lambda state: state.known_senders.add("name:b"))
        first.update(lambda state: state.processed_opt_outs.append("m"))
        reloaded = SafetyStore(tmp_path / "state.json").state
        assert reloaded.opted_out == {"name:a"}
        assert reloaded.known_senders == {"name:b"}
        assert reloaded.processed_opt_outs == ["m"]

    def test_works_without_fcntl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(whatsapp_web, "fcntl", None)
        store = SafetyStore(tmp_path / "state.json")
        store.update(lambda state: state.opted_out.add("name:a"))
        assert SafetyStore(tmp_path / "state.json").state.opted_out == {"name:a"}

    def test_deleted_file_keeps_memory_state(self, tmp_path):
        store = SafetyStore(tmp_path / "state.json")
        store.update(lambda state: state.opted_out.add("name:a"))
        (tmp_path / "state.json").unlink()
        store.refresh()
        assert store.state.opted_out == {"name:a"}


class TestPersistentSendHistory:
    def test_add_prunes_old_entries_and_sorts(self, tmp_path):
        store = SafetyStore(tmp_path / "state.json")
        history = PersistentSendHistory(store)
        history.add(1_000_000.0)
        history.add(1_000_500.0)
        history.add(1_000_000.0 + SECONDS_PER_DAY + 1000)
        history.add(1_000_100.0 + SECONDS_PER_DAY + 2000)
        assert history.times() == sorted(history.times())
        assert 1_000_000.0 not in history.times()


class TestFutureDatedEntries:
    def test_guard_drops_entries_beyond_skew_on_start(self, tmp_path, caplog):
        clock = FakeClock()
        store = SafetyStore(tmp_path / "state.json")
        store.update(lambda s: s.send_times.extend([clock.now() + 10, clock.now() + 99_999]))
        with caplog.at_level(logging.WARNING):
            guard = SafetyGuard(SafetyConfig(), store, clock.as_clock())
        assert guard.send_times == [clock.now() + 10]
        assert "future" in caplog.text
        assert CLOCK_SKEW_TOLERANCE_S > 10


class TestNewRecipientCap:
    def test_blocks_after_cap_but_allows_repeat_recipient(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_new_recipients_per_day=2)
        for recipient in ("a", "b"):
            guard.check_can_send([recipient], None)
            guard.record_send([recipient], None)
        guard.check_can_send(["a"], None)
        guard.record_send(["a"], None)  # repeat: not counted twice
        with pytest.raises(SafetyViolationError, match="New-recipient"):
            guard.check_can_send(["c"], None)

    def test_known_senders_are_exempt(self, tmp_path):
        guard, store, _ = make_guard(tmp_path, max_new_recipients_per_day=1)
        guard.record_send(["a"], None)
        guard.note_incoming("c", "m1", "hello")
        guard.check_can_send(["c"], None)
        guard.record_send(["c"], None)
        assert len(store.state.new_recipients) == 1

    def test_cap_resets_after_a_day(self, tmp_path):
        guard, store, clock = make_guard(tmp_path, max_new_recipients_per_day=1)
        guard.record_send(["a"], None)
        clock.time += SECONDS_PER_DAY - 1
        with pytest.raises(SafetyViolationError):
            guard.check_can_send(["b"], None)
        clock.time += 1
        guard.check_can_send(["b"], None)
        guard.record_send(["b"], None)
        assert [key for _, key in store.state.new_recipients] == ["b"]

    def test_many_recipients_at_once_are_counted_together(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_new_recipients_per_day=2)
        with pytest.raises(SafetyViolationError, match="New-recipient"):
            guard.check_can_send(["a", "b", "c"], None)

    def test_name_and_phone_share_one_count_once_linked(self, tmp_path):
        guard, store, _ = make_guard(tmp_path, max_new_recipients_per_day=1)
        guard.link_identities("phone:1", "name:alice")
        guard.record_send(["phone:1"], None)
        guard.check_can_send(["name:alice"], None)  # same person: not a new one
        guard.record_send(["name:alice"], None)
        assert len(store.state.new_recipients) == 1


class TestIdenticalMessageCap:
    def test_blocks_extra_distinct_recipient_with_normalised_text(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_identical_recipients=2)
        guard.record_send(["a"], "Hello  World")
        guard.record_send(["b"], "hello world")
        with pytest.raises(SafetyViolationError, match="Identical"):
            guard.check_can_send(["c"], " HELLO world ")
        guard.check_can_send(["a"], "hello world")
        guard.check_can_send(["c"], "different text")

    def test_old_broadcasts_expire(self, tmp_path):
        guard, store, clock = make_guard(tmp_path, max_identical_recipients=1)
        guard.record_send(["a"], "hi")
        clock.time += SECONDS_PER_DAY
        guard.check_can_send(["b"], "hi")
        guard.record_send(["b"], "hi")
        assert len(store.state.broadcasts) == 1

    def test_no_content_skips_the_check(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_identical_recipients=1)
        guard.record_send(["a"], None)
        guard.check_can_send(["b"], None)


class TestGroupCap:
    def test_blocks_after_cap_and_resets_next_day(self, tmp_path):
        guard, _, clock = make_guard(tmp_path, max_groups_per_day=1)
        guard.check_can_send(["a"], None, creates_group=True)
        guard.record_send(["a"], None, creates_group=True)
        with pytest.raises(SafetyViolationError, match="Group-creation"):
            guard.check_can_send(["b"], None, creates_group=True)
        guard.check_can_send(["b"], None)  # ordinary sends are unaffected
        clock.time += SECONDS_PER_DAY
        guard.check_can_send(["b"], None, creates_group=True)


class TestOptOut:
    @pytest.mark.parametrize(
        "text",
        ["  STOP ", "STOP.", "Stop!", "stop 🙏", "🛑 stop", "Unsubscribe", "stop sending me messages", "STOP, please"],
    )
    def test_keyword_variants_opt_out(self, tmp_path, text):
        guard, _, _ = make_guard(tmp_path)
        guard.note_incoming("a", "m1", text)
        assert guard.is_opted_out("a")
        with pytest.raises(SafetyViolationError, match="opted out"):
            guard.check_can_send(["a"], "hi")

    @pytest.mark.parametrize("text", ["please stop", "stopping", "hello", "", "!!!", "bus stop"])
    def test_other_text_does_not_opt_out(self, tmp_path, text):
        guard, _, _ = make_guard(tmp_path)
        guard.note_incoming("a", "m1", text)
        assert not guard.is_opted_out("a")
        assert guard.has_messaged_us("a")

    def test_opt_in_survives_rereading_the_same_stop_message(self, tmp_path):
        guard, _, _ = make_guard(tmp_path)
        guard.note_incoming("a", "m1", "stop")
        guard.opt_in("a")
        guard.note_incoming("a", "m1", "stop")
        assert not guard.is_opted_out("a")
        guard.note_incoming("a", "m2", "unsubscribe")
        assert guard.is_opted_out("a")

    def test_processed_opt_out_history_is_capped(self, tmp_path):
        guard, store, _ = make_guard(tmp_path)
        for index in range(MAX_PROCESSED_OPT_OUTS + 10):
            guard.note_incoming("a", f"m{index}", "stop")
        assert len(store.state.processed_opt_outs) == MAX_PROCESSED_OPT_OUTS

    def test_opt_out_applies_across_linked_identities(self, tmp_path):
        guard, _, _ = make_guard(tmp_path)
        guard.opt_out("name:alice")
        guard.link_identities("phone:1", "name:alice")
        assert guard.is_opted_out("phone:1")
        with pytest.raises(SafetyViolationError):
            guard.check_can_send(["phone:1"], "hi")
        guard.opt_in("phone:1")
        assert not guard.is_opted_out("name:alice")

    def test_manual_opt_out_and_in(self, tmp_path):
        guard, _, _ = make_guard(tmp_path)
        guard.opt_out("a")
        assert guard.is_opted_out("a")
        guard.opt_in("a")
        assert not guard.is_opted_out("a")

    def test_custom_keywords_and_punctuation_only_keywords_are_ignored(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, opt_out_keywords=frozenset({"quit", "!!!"}))
        guard.note_incoming("a", "m1", "stop")
        guard.note_incoming("b", "m2", "")
        assert not guard.is_opted_out("a") and not guard.is_opted_out("b")
        guard.note_incoming("a", "m3", "QUIT")
        assert guard.is_opted_out("a")

    def test_repeat_note_of_known_sender_does_not_rewrite_state(self, tmp_path):
        guard, _, _ = make_guard(tmp_path)
        guard.note_incoming("a", "m1", "hi")
        (tmp_path / "state.json").unlink()
        guard.note_incoming("a", "m2", "hi again")
        assert not (tmp_path / "state.json").exists()


class TestBreaker:
    def test_trip_blocks_and_reset_unblocks(self, tmp_path, caplog):
        guard, _, _ = make_guard(tmp_path)
        with caplog.at_level(logging.WARNING):
            guard.trip_breaker("test reason")
        assert "test reason" in caplog.text
        with pytest.raises(AccountAtRiskError, match="test reason"):
            guard.check_can_send(["a"], None)
        guard.reset_breaker()
        guard.check_can_send(["a"], None)

    def test_consecutive_failures_trip_at_threshold(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_consecutive_failures=3)
        guard.record_failure()
        guard.record_failure()
        guard.check_can_send(["a"], None)
        guard.record_failure()
        with pytest.raises(AccountAtRiskError, match="3 consecutive"):
            guard.check_can_send(["a"], None)

    def test_success_resets_failure_streak(self, tmp_path):
        guard, _, _ = make_guard(tmp_path, max_consecutive_failures=2)
        guard.record_failure()
        guard.record_success()
        guard.record_success()  # nothing to reset: no-op
        guard.record_failure()
        guard.check_can_send(["a"], None)

    def test_breaker_state_survives_restart_and_is_seen_by_other_instances(self, tmp_path):
        guard, _, clock = make_guard(tmp_path)
        other, _, _ = make_guard(tmp_path, clock)
        guard.trip_breaker("persisted")
        with pytest.raises(AccountAtRiskError, match="persisted"):
            other.check_can_send(["a"], None)
