from datetime import date, datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor


class DayNotifierTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 11, 13)
        self.key = monitor.day_key(self.today)

    def event(self, day, *, ID=-2, date_end=None):
        return {"ID": ID, "Date": monitor.day_key(day), "DateEnd": monitor.day_key(date_end or day),
                "Start_Time": "10:00", "End_Time": "11:00", "Title": "آقایان"}

    def test_known_conversion_from_user_saved_page(self):
        self.assertEqual(monitor.day_key(date(2026, 12, 5)), "1405/09/14")
        self.assertEqual(monitor.parse_date("۱۴۰۵/۰۹/۱۴"), date(2026, 12, 5))
        self.assertEqual(monitor.parse_date("2026-12-05"), date(2026, 12, 5))
        self.assertEqual(monitor.day_key(date(2026, 3, 21)), "1405/01/01")

    def test_first_run_sends_each_open_day_once_even_with_multiple_slots(self):
        days = [self.today + timedelta(days=i) for i in (2, 0, 1, 1)]
        available = monitor.extract_open_days([self.event(day) for day in days], self.today)
        sent, persist = Mock(), Mock()
        delivered = monitor.process_days(available, set(), sent, persist)
        self.assertEqual(delivered, [monitor.day_key(self.today + timedelta(days=i)) for i in range(3)])
        self.assertEqual(sent.call_count, 3)
        self.assertIn('رزرو روز', sent.call_args_list[0].args[0])
        self.assertEqual(persist.call_count, 3)

    def test_two_new_days_are_sent_in_order_as_two_individual_messages(self):
        old_days = {monitor.day_key(self.today)}
        available = [monitor.day_key(self.today + timedelta(days=i)) for i in (3, 1, 0, 2)]
        sent = Mock()
        newly_sent = monitor.process_days(available, old_days, sent, lambda x: None)
        self.assertEqual(newly_sent, [monitor.day_key(self.today + timedelta(days=i)) for i in (1, 2, 3)])
        self.assertEqual(sent.call_count, 3)
        self.assertEqual(old_days, set(available))

    def test_no_repeat_when_date_disappears_then_returns(self):
        state = set()
        a = monitor.day_key(self.today)
        b = monitor.day_key(self.today + timedelta(days=1))
        sent = Mock()
        for batch in ([a], [], [a, b], [a, b]):
            monitor.process_days(batch, state, sent, lambda x: None)
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(state, {a, b})

    def test_old_dates_and_fully_booked_events_are_not_announced(self):
        earlier = self.today - timedelta(days=1)
        next_day = self.today + timedelta(days=1)
        events = [self.event(earlier), self.event(self.today, ID=123), self.event(next_day, ID=-2)]
        self.assertEqual(monitor.extract_open_days(events, self.today), [monitor.day_key(next_day)])

    def test_persian_date_span_expands_including_today(self):
        calendar = [self.event(self.today-timedelta(days=1), date_end=self.today+timedelta(days=2))]
        self.assertEqual(monitor.extract_open_days(calendar, self.today),
                         [monitor.day_key(self.today + timedelta(days=i)) for i in range(3)])

    def test_persists_only_after_successful_telegram_send(self):
        d1, d2 = self.key, monitor.day_key(self.today + timedelta(days=1))
        state = set()
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp) / 'state.json'
            def failing_send(message):
                if str(monitor.format_day(d2)) in message:
                    raise ConnectionError('Simulated failure')
            with self.assertRaises(ConnectionError):
                monitor.process_days([d1, d2], state, failing_send,
                                     lambda keys: monitor.save_state(keys, location))
            self.assertEqual(monitor.load_state(location), {d1})
            sent = Mock()
            monitor.process_days([d1, d2], monitor.load_state(location), sent,
                                 lambda keys: monitor.save_state(keys, location))
            self.assertEqual(sent.call_count, 1)
            self.assertEqual(monitor.load_state(location), {d1, d2})

    def test_telegram_channel_send_uses_one_send_message_per_day(self):
        with patch.dict(monitor.os.environ, {"TELEGRAM_BOT_TOKEN": "TEST_TOKEN", "TELEGRAM_CHAT_ID": "@example_channel"}), \
             patch.object(monitor.requests, "post") as req:
            req.return_value.json.return_value = {"ok": True, "result": {"message_id": 1}}
            msg = monitor.message_for_day(self.key)
            monitor.telegram_send(msg)
            self.assertEqual(req.call_count, 1)
            self.assertEqual(req.call_args.args[0], "https://api.telegram.org/botTEST_TOKEN/sendMessage")
            self.assertEqual(req.call_args.kwargs["json"]["chat_id"], "@example_channel")
            self.assertEqual(req.call_args.kwargs["json"]["text"], msg)

    def test_end_to_end_multiple_runs_with_fake_site_and_fake_telegram(self):
        now = datetime.now(monitor.TEHRAN).date()
        def sample(*days):
            return [{"ID": -2, "Date": monitor.day_key(now + timedelta(days=d))} for d in days]
        all_sent = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(monitor, 'STATE_PATH', Path(tmp) / 'history.json'), \
             patch.object(monitor, 'fetch_calendar') as fetcher, \
             patch.object(monitor, 'telegram_send', side_effect=all_sent.append), \
             patch.object(monitor.time, 'sleep'):
            fetcher.return_value = sample(2, 0, 1, 1)
            monitor.run()
            self.assertEqual(len(all_sent), 3)
            fetcher.return_value = sample(2, 0, 1, 4, 3)
            monitor.run()
            self.assertEqual(len(all_sent), 5)
            fetcher.return_value = sample(2, 0, 1, 4, 3)
            monitor.run()
            self.assertEqual(len(all_sent), 5)
            self.assertEqual(monitor.load_state(), {monitor.day_key(now + timedelta(days=d)) for d in range(5)})
            self.assertEqual(all_sent[3], monitor.message_for_day(monitor.day_key(now+timedelta(days=3))))
            self.assertEqual(all_sent[4], monitor.message_for_day(monitor.day_key(now+timedelta(days=4))))

    def test_api_format_and_calendar_validation(self):
        session = Mock()
        resp = Mock()
        resp.json.return_value = {'Result': 0, 'calendarList': [{'ID': -2, 'Date': self.key}]}
        session.post.return_value = resp
        # both start/bookableLoad2 same mocked response, sufficient for transport contract
        calendar = monitor.fetch_calendar(session)
        self.assertEqual(len(calendar), 1)
        self.assertEqual(session.post.call_count, 2)
        args, kwargs = session.post.call_args
        payload = json.loads(kwargs['files']['Input'][1])
        self.assertEqual(payload['FN'], 'bookableLoad2')
        self.assertEqual(payload['ID'], 10499)

    def test_dry_run_does_not_send_or_persist(self):
        with patch.object(monitor, 'fetch_calendar', return_value=[self.event(self.today)]), \
             patch.object(monitor, 'load_state', return_value=set()), \
             patch.object(monitor, 'telegram_send') as sender, \
             patch.object(monitor, 'save_state') as saver, \
             patch.object(monitor, 'TEHRAN', monitor.ZoneInfo('Asia/Tehran')):
            # Only assert no side effects, irrespective of actual current date
            monitor.run(dry_run=True)
            sender.assert_not_called()
            saver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
