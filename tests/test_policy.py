import unittest
from dataclasses import replace
from datetime import datetime, timezone

from heartbeat.policy import Settings, candidate_sessions, contact_block, wake_block
from heartbeat.state import HeartbeatState, SessionState

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc).timestamp()
UMO = "qq:FriendMessage:1"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(timezone="UTC")
        self.session = SessionState(last_user_at=NOW - 20000)
        self.state = HeartbeatState(sessions={UMO: self.session})

    def contact(self, **kwargs):
        return contact_block(
            self.session, self.settings, NOW, started_user_at=self.session.last_user_at, **kwargs
        )

    def test_real_elapsed_intervals_and_restart_deadlines(self):
        for minutes in (45, 90, 120, 1440):
            with self.subTest(minutes=minutes):
                settings = replace(
                    self.settings, interval_minutes=minutes, background_outside_active_hours=True
                )
                self.state.begin(UMO, NOW, minutes, settings.day(NOW))
                self.assertEqual(self.session.next_due_at, NOW + minutes * 60)
                self.assertEqual(
                    wake_block(self.state, UMO, settings, NOW + minutes * 60 - 1), "not_due"
                )
                self.assertEqual(wake_block(self.state, UMO, settings, NOW + minutes * 60), "")

    def test_all_rotates_to_least_recently_woken(self):
        other = "qq:FriendMessage:2"
        self.state.sessions[other] = SessionState()
        first = candidate_sessions(self.state, self.settings)[0]
        self.state.begin(first, NOW, 90, self.settings.day(NOW))
        self.assertNotEqual(candidate_sessions(self.state, self.settings)[0], first)

    def test_last_mode_only_uses_last_real_user_session(self):
        self.state.last_umo = UMO
        self.assertEqual(
            candidate_sessions(self.state, replace(self.settings, target_mode="last")), [UMO]
        )

    def test_recent_chat_blocks_contact_but_not_background(self):
        self.session.last_user_at = NOW - 10
        self.assertEqual(self.contact(), "recent_user_interaction")
        self.assertEqual(wake_block(self.state, UMO, self.settings, NOW), "")

    def test_explicit_quiet_blocks_contact_but_not_background(self):
        self.session.quiet_until = NOW + 7200
        self.assertEqual(self.contact(), "user_quiet_period")
        self.assertEqual(wake_block(self.state, UMO, self.settings, NOW), "")

    def test_background_can_run_outside_contact_hours(self):
        settings = replace(
            self.settings,
            active_hours_start="13:00",
            active_hours_end="14:00",
            background_outside_active_hours=True,
        )
        self.assertEqual(wake_block(self.state, UMO, settings, NOW), "")
        self.assertEqual(
            contact_block(self.session, settings, NOW, started_user_at=self.session.last_user_at),
            "outside_contact_hours",
        )

    def test_new_message_invalidates_old_contact_decision(self):
        self.assertEqual(
            contact_block(
                self.session, self.settings, NOW, started_user_at=self.session.last_user_at - 1
            ),
            "new_user_message",
        )

    def test_unanswered_contacts_back_off_without_escalating(self):
        self.session.last_contact_at = NOW - 25 * 3600
        self.session.unanswered = 1
        self.assertEqual(self.contact(), "")
        self.session.unanswered = 2
        self.assertEqual(self.contact(), "unanswered_backoff")
        self.session.observe_user("1", NOW - 3600)
        self.assertEqual(self.session.unanswered, 0)
        self.assertEqual(self.contact(), "")

    def test_daily_topic_and_exact_message_dedup_survive_user_reply(self):
        self.session.record_contact(
            NOW - 20000, self.settings.day(NOW), "lunch", "digest", "reason"
        )
        self.session.observe_user("1", NOW - 3600)
        self.assertEqual(self.contact(topic="lunch"), "already_contacted_today")
        self.assertEqual(self.contact(topic="changed", digest="digest"), "already_contacted_today")

    def test_separate_wake_and_message_budgets(self):
        self.state.wake_day = self.settings.day(NOW)
        self.state.wake_count = 48
        self.assertEqual(wake_block(self.state, UMO, self.settings, NOW), "wake_budget")
        self.assertEqual(self.contact(), "")
        self.state.wake_count = 0
        self.session.contacts = [{"day": self.settings.day(NOW)}] * 3
        self.assertEqual(self.contact(), "message_budget")
        self.assertEqual(wake_block(self.state, UMO, self.settings, NOW), "")

    def test_midnight_uses_configured_timezone(self):
        near_midnight = datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc).timestamp()
        settings = replace(self.settings, timezone="America/New_York")
        self.assertEqual(settings.day(near_midnight), "2026-10-25")
        self.assertEqual(settings.local_time(near_midnight).hour, 20)
        self.state.wake_day = "2026-10-24"
        self.state.wake_count = 48
        self.state.roll_day(settings.day(near_midnight))
        self.assertEqual(self.state.wake_count, 0)

    def test_cross_midnight_and_equal_hours(self):
        self.assertFalse(
            replace(self.settings, active_hours_start="22:00", active_hours_end="06:00").in_hours(
                NOW
            )
        )
        self.assertTrue(
            replace(self.settings, active_hours_start="00:00", active_hours_end="00:00").in_hours(
                NOW
            )
        )

    def test_invalid_configuration_fails_clearly(self):
        for config in (
            {"active_hours_end": "25:00"},
            {"interval_minutes": 0},
            {"target_mode": "unknown"},
            {"check_last_interaction": "false"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                Settings.from_config(config)

    def test_paused_and_group_sessions_do_not_wake(self):
        self.state.paused = True
        self.assertEqual(wake_block(self.state, UMO, self.settings, NOW, manual=True), "paused")
        self.state.paused = False
        group = "qq:GroupMessage:3"
        self.state.sessions[group] = SessionState()
        self.assertEqual(
            wake_block(self.state, group, self.settings, NOW), "group_sessions_disabled"
        )
