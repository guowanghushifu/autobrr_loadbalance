import sys
import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock


sys.modules.setdefault("qbittorrentapi", types.SimpleNamespace(Client=object))
sys.modules.setdefault("requests", types.SimpleNamespace(get=None))
sys.modules.setdefault(
    "flask",
    types.SimpleNamespace(
        Flask=object,
        request=types.SimpleNamespace(get_json=lambda: None),
        jsonify=lambda *args, **kwargs: None,
        render_template=lambda *args, **kwargs: None,
    ),
)

import main


class UploadSpeedSortValueTest(unittest.TestCase):
    def _balancer_with_primary_sort_key(self, sort_key):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.config = {"primary_sort_key": sort_key}
        return balancer

    def _instance_with_speeds(self, upload_speed=0.0, download_speed=0.0):
        return main.InstanceInfo(
            name="test",
            url="http://example.invalid",
            username="user",
            password="pass",
            upload_speed=upload_speed,
            download_speed=download_speed,
        )

    def test_upload_speed_below_threshold_kib_is_sorted_as_zero(self):
        balancer = self._balancer_with_primary_sort_key("upload_speed")
        instance = self._instance_with_speeds(
            upload_speed=main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB - 0.1
        )

        self.assertEqual(0.0, balancer._get_primary_sort_value(instance))

    def test_upload_speed_at_threshold_kib_keeps_actual_value(self):
        balancer = self._balancer_with_primary_sort_key("upload_speed")
        instance = self._instance_with_speeds(
            upload_speed=main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB
        )

        self.assertEqual(
            main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB,
            balancer._get_primary_sort_value(instance),
        )

    def test_download_speed_sorting_is_not_thresholded(self):
        balancer = self._balancer_with_primary_sort_key("download_speed")
        instance = self._instance_with_speeds(upload_speed=199.9, download_speed=123.4)

        self.assertEqual(123.4, balancer._get_primary_sort_value(instance))

    def test_upload_download_speed_sorting_combines_upload_sort_value_and_download_speed(self):
        balancer = self._balancer_with_primary_sort_key("upload_download_speed")
        instance = self._instance_with_speeds(
            upload_speed=main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB,
            download_speed=123.4,
        )

        self.assertEqual(
            main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB
            * main.UPLOAD_DOWNLOAD_SORT_UPLOAD_WEIGHT
            + 123.4 * main.UPLOAD_DOWNLOAD_SORT_DOWNLOAD_WEIGHT,
            balancer._get_primary_sort_value(instance),
        )

    def test_upload_download_speed_sorting_treats_low_upload_as_zero(self):
        balancer = self._balancer_with_primary_sort_key("upload_download_speed")
        instance = self._instance_with_speeds(
            upload_speed=main.UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB - 0.1,
            download_speed=123.4,
        )

        self.assertEqual(
            123.4 * main.UPLOAD_DOWNLOAD_SORT_DOWNLOAD_WEIGHT,
            balancer._get_primary_sort_value(instance),
        )

    def test_total_downloads_sorting_combines_active_and_waiting_downloads(self):
        balancer = self._balancer_with_primary_sort_key("total_downloads")
        instance = self._instance_with_speeds()
        instance.active_downloads = 3
        instance.new_tasks_count = 10
        instance.waiting_downloads_count = 4

        self.assertEqual(5.0, balancer._get_primary_sort_value(instance))


class AddTorrentRefreshTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, result="Ok."):
            self.result = result
            self.add_params = None

        def torrents_add(self, **kwargs):
            self.add_params = kwargs
            return self.result

    def _balancer(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.config = {"debug_add_stopped": False}
        balancer.status_refresh_event = threading.Event()
        return balancer

    def _instance(self, client):
        return main.InstanceInfo(
            name="test",
            url="http://example.invalid",
            username="user",
            password="pass",
            client=client,
            active_downloads=2,
        )

    def test_successful_add_optimistically_increments_active_downloads_and_requests_refresh(self):
        balancer = self._balancer()
        instance = self._instance(self.FakeClient())
        torrent = main.PendingTorrent(
            download_url="magnet:?xt=urn:btih:test",
            release_name="test-release",
            category="movies",
        )

        self.assertTrue(balancer._add_torrent_to_instance(instance, torrent))

        self.assertEqual(3, instance.active_downloads)
        self.assertTrue(balancer.status_refresh_event.is_set())

    def test_requested_status_refresh_waits_before_returning_to_refresh_loop(self):
        balancer = self._balancer()
        balancer.status_refresh_event.set()

        with mock.patch.object(main.time, "sleep") as sleep:
            balancer._wait_for_next_status_refresh(30)

        sleep.assert_called_once_with(main.STATUS_REFRESH_AFTER_ADD_DELAY)


class InstanceMetricsLoggingTest(unittest.TestCase):
    def test_speed_log_uses_mb_for_rates_at_least_one_mib(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        instance = main.InstanceInfo(
            name="test",
            url="http://example.invalid",
            username="user",
            password="pass",
        )
        maindata = {
            "server_state": {
                "up_info_speed": int(1.5 * main.BYTES_TO_KB * main.BYTES_TO_KB),
                "dl_info_speed": 512 * main.BYTES_TO_KB,
                "alltime_ul": 3_000_000_000_000,
                "alltime_dl": 4_000_000_000_000,
                "free_space_on_disk": 100 * main.BYTES_TO_GB,
            },
            "torrents": {},
        }

        with mock.patch.object(main.logger, "debug") as debug:
            balancer._update_instance_metrics(instance, maindata)

        log_message = debug.call_args.args[0]
        self.assertIn("上传=1.5MB/s", log_message)
        self.assertIn("下载=512.0KB/s", log_message)
        self.assertEqual(3_000_000_000_000, instance.total_uploaded_bytes)
        self.assertEqual(4_000_000_000_000, instance.total_downloaded_bytes)

    def test_metrics_count_waiting_downloads_from_qbittorrent_states(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        instance = main.InstanceInfo(
            name="test",
            url="http://example.invalid",
            username="user",
            password="pass",
        )
        maindata = {
            "server_state": {},
            "torrents": {
                "active": types.SimpleNamespace(state="downloading"),
                "stalled": types.SimpleNamespace(state="stalledDL"),
                "queued": types.SimpleNamespace(state="queuedDL"),
                "metadata": types.SimpleNamespace(state="metaDL"),
                "paused": types.SimpleNamespace(state="pausedDL"),
            },
        }

        balancer._update_instance_metrics(instance, maindata)

        self.assertEqual(1, instance.active_downloads)
        self.assertEqual(3, instance.waiting_downloads_count)

    def test_metrics_are_aggregated_by_tracker_host(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        instance = main.InstanceInfo(name="test", url="http://example.invalid", username="user", password="pass")
        maindata = {
            "server_state": {},
            "torrents": {
                "one": types.SimpleNamespace(state="downloading", tracker="https://tracker.example/announce", upspeed=1024, dlspeed=2048, uploaded=2_000_000_000_000, downloaded=3_000_000_000_000),
                "two": types.SimpleNamespace(state="stalledUP", tracker="https://tracker.example/announce", upspeed=512, dlspeed=0, uploaded=500_000_000_000, downloaded=250_000_000_000),
                "three": types.SimpleNamespace(state="pausedDL", tracker="", upspeed=0, dlspeed=0, uploaded=0, downloaded=0),
            },
        }

        balancer._update_instance_metrics(instance, maindata)

        self.assertEqual(2, instance.tracker_stats["tracker.example"]["torrent_count"])
        self.assertEqual(1, instance.tracker_stats["tracker.example"]["active_downloads"])
        self.assertEqual(1.5, instance.tracker_stats["tracker.example"]["upload_speed_kib"])
        self.assertEqual(2.0, instance.tracker_stats["tracker.example"]["download_speed_kib"])
        self.assertEqual(2_500_000_000_000, instance.tracker_stats["tracker.example"]["uploaded_bytes"])
        self.assertEqual(3_250_000_000_000, instance.tracker_stats["tracker.example"]["downloaded_bytes"])
        self.assertEqual(1, instance.tracker_stats["无 tracker"]["torrent_count"])


class StatusUpdateTest(unittest.TestCase):
    class FakeClient:
        def sync_maindata(self):
            return {
                "server_state": {
                    "up_info_speed": 0,
                    "dl_info_speed": 0,
                    "free_space_on_disk": 0,
                },
                "torrents": {},
            }

    def test_single_instance_update_refreshes_metrics_from_maindata(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.config = {}
        instance = main.InstanceInfo(
            name="test",
            url="http://example.invalid",
            username="user",
            password="pass",
            client=self.FakeClient(),
        )

        balancer._update_single_instance(instance)

        self.assertEqual(1, instance.success_metrics_count)

    def test_status_refresh_records_separate_upload_and_download_history(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.config = {}
        balancer.instances_lock = threading.Lock()
        balancer.metrics_history_lock = threading.Lock()
        balancer.metrics_history = main.deque(maxlen=10)
        balancer.instances = [main.InstanceInfo(
            name="test", url="http://example.invalid", username="user", password="pass",
            client=self.FakeClient(), is_connected=True,
        )]

        balancer._update_instance_status()

        self.assertEqual(0.0, balancer.metrics_history[0]['upload_speed_kib'])
        self.assertEqual(0.0, balancer.metrics_history[0]['download_speed_kib'])
        self.assertIn('timestamp', balancer.metrics_history[0])


class DashboardTrafficSnapshotTest(unittest.TestCase):
    def test_snapshot_exposes_instance_and_tracker_cumulative_traffic(self):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.instances = [
            main.InstanceInfo(
                name="test",
                url="http://example.invalid",
                username="user",
                password="pass",
                total_uploaded_bytes=3_000_000_000_000,
                total_downloaded_bytes=4_000_000_000_000,
                today_uploaded_bytes=30_000_000_000,
                today_downloaded_bytes=40_000_000_000,
                tracker_stats={
                    "tracker.example": {
                        "torrent_count": 2,
                        "active_downloads": 1,
                        "upload_speed_kib": 1000.0,
                        "download_speed_kib": 2000.0,
                        "uploaded_bytes": 2_500_000_000_000,
                        "downloaded_bytes": 3_250_000_000_000,
                        "today_uploaded_bytes": 25_000_000_000,
                        "today_downloaded_bytes": 32_000_000_000,
                    }
                },
            ),
            main.InstanceInfo(
                name="test-2",
                url="http://example-2.invalid",
                username="user",
                password="pass",
                total_uploaded_bytes=1_000_000_000_000,
                total_downloaded_bytes=2_000_000_000_000,
                today_uploaded_bytes=10_000_000_000,
                today_downloaded_bytes=20_000_000_000,
                tracker_stats={
                    "tracker.example": {
                        "torrent_count": 3,
                        "active_downloads": 2,
                        "upload_speed_kib": 500.0,
                        "download_speed_kib": 750.0,
                        "uploaded_bytes": 1_000_000_000_000,
                        "downloaded_bytes": 2_000_000_000_000,
                        "today_uploaded_bytes": 10_000_000_000,
                        "today_downloaded_bytes": 20_000_000_000,
                    }
                },
            ),
        ]
        balancer.instances_lock = threading.Lock()
        balancer.metrics_history = []
        balancer.metrics_history_lock = threading.Lock()
        balancer.pending_torrents = []
        balancer.pending_torrents_lock = threading.Lock()
        balancer.config = {"qbittorrent_instances": [], "webhook_ip_whitelist": []}
        balancer.config_lock = threading.Lock()
        balancer.telegram_notifier = None

        snapshot = balancer.get_dashboard_snapshot()

        self.assertEqual(4_000_000_000_000, snapshot["traffic_totals"]["uploaded_bytes"])
        self.assertEqual(6_000_000_000_000, snapshot["traffic_totals"]["downloaded_bytes"])
        self.assertEqual(40_000_000_000, snapshot["traffic_totals"]["today_uploaded_bytes"])
        self.assertEqual(60_000_000_000, snapshot["traffic_totals"]["today_downloaded_bytes"])
        self.assertEqual(3_500_000_000_000, snapshot["tracker_stats"][0]["uploaded_bytes"])
        self.assertEqual(5_250_000_000_000, snapshot["tracker_stats"][0]["downloaded_bytes"])
        self.assertEqual(
            [{"name": "test", "torrent_count": 2}, {"name": "test-2", "torrent_count": 3}],
            snapshot["tracker_stats"][0]["instance_torrent_counts"],
        )


class DailyTrafficTest(unittest.TestCase):
    def _balancer(self, current):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.daily_traffic_lock = threading.Lock()
        balancer.daily_traffic_last_saved = 0.0
        balancer.daily_traffic_state = balancer._empty_daily_traffic_state(current)
        return balancer

    def test_daily_traffic_accumulates_deltas_and_resets_on_local_midnight(self):
        current = main.datetime(2026, 7, 14, 8, 0, tzinfo=main.ZoneInfo('Asia/Shanghai'))
        balancer = self._balancer(current)
        instance = main.InstanceInfo(name='test', url='http://qb', username='user', password='pass', total_uploaded_bytes=1000, total_downloaded_bytes=2000)
        torrent = types.SimpleNamespace(tracker='https://tracker.example/announce', uploaded=300, downloaded=400, added_on=current.timestamp() - 86400)
        torrents = {'hash': torrent}
        instance.tracker_stats = balancer._aggregate_tracker_stats(torrents.values())

        balancer._update_daily_traffic(instance, torrents, current)
        self.assertEqual(0, instance.today_uploaded_bytes)
        self.assertEqual(0, instance.tracker_stats['tracker.example']['today_uploaded_bytes'])

        instance.total_uploaded_bytes = 1500
        instance.total_downloaded_bytes = 2600
        torrent.uploaded = 500
        torrent.downloaded = 700
        instance.tracker_stats = balancer._aggregate_tracker_stats(torrents.values())
        balancer._update_daily_traffic(instance, torrents, current.replace(hour=9))

        self.assertEqual(500, instance.today_uploaded_bytes)
        self.assertEqual(600, instance.today_downloaded_bytes)
        self.assertEqual(200, instance.tracker_stats['tracker.example']['today_uploaded_bytes'])
        self.assertEqual(300, instance.tracker_stats['tracker.example']['today_downloaded_bytes'])

        instance.tracker_stats = balancer._aggregate_tracker_stats(torrents.values())
        balancer._update_daily_traffic(instance, torrents, current.replace(day=15, hour=0, minute=1))
        self.assertEqual(0, instance.today_uploaded_bytes)
        self.assertEqual(0, instance.tracker_stats['tracker.example']['today_uploaded_bytes'])

    def test_new_torrent_added_today_counts_traffic_before_first_poll(self):
        current = main.datetime(2026, 7, 14, 8, 0, tzinfo=main.ZoneInfo('Asia/Shanghai'))
        balancer = self._balancer(current)
        instance = main.InstanceInfo(name='test', url='http://qb', username='user', password='pass')
        torrent = types.SimpleNamespace(tracker='https://tracker.example/announce', uploaded=300, downloaded=400, added_on=current.timestamp() - 60)
        torrents = {'new-hash': torrent}
        instance.tracker_stats = balancer._aggregate_tracker_stats(torrents.values())

        balancer._update_daily_traffic(instance, torrents, current)

        self.assertEqual(300, instance.tracker_stats['tracker.example']['today_uploaded_bytes'])
        self.assertEqual(400, instance.tracker_stats['tracker.example']['today_downloaded_bytes'])

    def test_daily_traffic_state_survives_restart(self):
        current = main.datetime.now(main.ZoneInfo('Asia/Shanghai'))
        with tempfile.TemporaryDirectory() as directory:
            balancer = self._balancer(current)
            balancer.log_dir = directory
            balancer.daily_traffic_state_file = os.path.join(directory, 'traffic.json')
            balancer.daily_traffic_state['instances']['test'] = {'today_uploaded_bytes': 123}
            with balancer.daily_traffic_lock:
                balancer._save_daily_traffic_state_locked(force=True)

            restored = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
            restored.config = {'dashboard': {'timezone': 'Asia/Shanghai'}}
            restored.daily_traffic_state_file = balancer.daily_traffic_state_file

            state = restored._load_daily_traffic_state()

            self.assertEqual(123, state['instances']['test']['today_uploaded_bytes'])


class DashboardConfigurationTest(unittest.TestCase):
    def _balancer(self, config, config_file):
        balancer = main.QBittorrentLoadBalancer.__new__(main.QBittorrentLoadBalancer)
        balancer.config = config
        balancer.config_file = config_file
        balancer.config_lock = threading.Lock()
        balancer.instances_lock = threading.Lock()
        balancer.pending_torrents_lock = threading.Lock()
        balancer.instances = []
        balancer.pending_torrents = []
        return balancer

    def test_empty_whitelist_allows_all_and_cidr_restricts_sources(self):
        balancer = self._balancer({'webhook_ip_whitelist': []}, os.devnull)
        self.assertTrue(balancer.is_webhook_ip_allowed('203.0.113.8'))

        balancer.config['webhook_ip_whitelist'] = ['192.168.10.0/24', '2001:db8::1']
        self.assertTrue(balancer.is_webhook_ip_allowed('192.168.10.42'))
        self.assertTrue(balancer.is_webhook_ip_allowed('2001:db8::1'))
        self.assertFalse(balancer.is_webhook_ip_allowed('192.168.11.42'))

    def test_whitelist_change_is_normalized_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, 'config.json')
            balancer = self._balancer({'webhook_ip_whitelist': []}, config_file)

            normalized = balancer.add_webhook_whitelist_entry('192.168.20.44/24')

            self.assertEqual('192.168.20.0/24', normalized)
            with open(config_file, encoding='utf-8') as handle:
                persisted = json.load(handle)
            self.assertEqual(['192.168.20.0/24'], persisted['webhook_ip_whitelist'])

    def test_import_old_config_preserves_dashboard_access_and_adds_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, 'config.json')
            current = {
                'dashboard': {'enabled': True, 'username': 'admin', 'password': 'secret'},
                'telegram': {'enabled': False},
                'webhook_ip_whitelist': ['10.0.0.0/8'],
                'connection_timeout': 1,
                'qbittorrent_instances': [],
            }
            balancer = self._balancer(current, config_file)
            old_config = {
                'qbittorrent_instances': [{
                    'name': 'legacy', 'url': 'http://qb:8080',
                    'username': 'admin', 'password': 'pass',
                }],
                'webhook_path': '/legacy-hook',
            }

            with mock.patch.object(balancer, '_connect_instance', side_effect=lambda instance: setattr(instance, 'is_connected', True)):
                result = balancer.import_config(old_config)

            self.assertEqual(1, result['connected'])
            self.assertTrue(result['restart_required'])
            self.assertEqual('admin', balancer.config['dashboard']['username'])
            self.assertEqual(['10.0.0.0/8'], balancer.config['webhook_ip_whitelist'])
            self.assertEqual(2, balancer.config['max_new_tasks_per_instance'])
            self.assertEqual('legacy', balancer.instances[0].name)

    def test_dashboard_telegram_update_preserves_blank_token_and_reloads_notifier(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, 'config.json')
            config = {
                'telegram': {'enabled': False, 'bot_token': 'saved-token', 'chat_id': 'old-chat'},
                'qbittorrent_instances': [],
            }
            balancer = self._balancer(config, config_file)
            old_notifier = mock.Mock()
            new_notifier = mock.Mock(enabled=True)
            balancer.telegram_notifier = old_notifier

            with mock.patch.object(main, 'TelegramNotifier', return_value=new_notifier):
                result = balancer.update_telegram_config({
                    'enabled': True, 'bot_token': '', 'chat_id': 'new-chat', 'timeout': 12,
                })

            old_notifier.stop.assert_called_once_with()
            self.assertIs(new_notifier, balancer.telegram_notifier)
            self.assertTrue(result['enabled'])
            self.assertEqual('saved-token', balancer.config['telegram']['bot_token'])
            self.assertEqual('new-chat', balancer.config['telegram']['chat_id'])

    def test_dashboard_timezone_is_validated_persisted_and_resets_today(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, 'config.json')
            balancer = self._balancer({'dashboard': {}, 'qbittorrent_instances': []}, config_file)
            balancer.log_dir = directory
            balancer.daily_traffic_state_file = os.path.join(directory, 'traffic.json')
            balancer.daily_traffic_lock = threading.Lock()
            balancer.daily_traffic_last_saved = 0.0
            balancer.daily_traffic_state = {'date': '2000-01-01', 'instances': {'old': {}}}
            balancer.instances = [main.InstanceInfo(name='test', url='http://qb', username='user', password='pass', today_uploaded_bytes=99)]

            result = balancer.update_dashboard_timezone('Europe/London')

            self.assertEqual('Europe/London', result['name'])
            self.assertTrue(balancer.config['dashboard']['timezone_configured'])
            self.assertEqual(0, balancer.instances[0].today_uploaded_bytes)
            with self.assertRaisesRegex(ValueError, 'invalid timezone'):
                balancer.update_dashboard_timezone('Not/A-Timezone')

    def test_clone_instance_preserves_secret_and_uses_unique_name(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, 'config.json')
            config = {'qbittorrent_instances': [{
                'name': 'source', 'url': 'http://qb:8080', 'username': 'admin',
                'password': 'secret', 'reserved_space': 0,
            }]}
            balancer = self._balancer(config, config_file)
            balancer.instances = [main.InstanceInfo(name='source', url='http://qb:8080', username='admin', password='secret')]

            with mock.patch.object(balancer, '_connect_instance', side_effect=lambda instance: setattr(instance, 'is_connected', True)):
                first = balancer.clone_qbittorrent_instance('source')
                second = balancer.clone_qbittorrent_instance('source')

            self.assertEqual('source-copy', first['name'])
            self.assertEqual('source-copy-2', second['name'])
            self.assertEqual('secret', balancer.config['qbittorrent_instances'][1]['password'])
            self.assertEqual(3, len(balancer.instances))


class DashboardLogHandlerTest(unittest.TestCase):
    def test_incremental_reads_return_only_records_after_cursor(self):
        handler = main.DashboardLogHandler(capacity=100)
        handler.emit(main.logging.LogRecord('test', main.logging.INFO, __file__, 1, 'first', (), None))
        first = handler.read_after()
        handler.emit(main.logging.LogRecord('test', main.logging.ERROR, __file__, 2, 'second', (), None))

        second = handler.read_after(first['cursor'])

        self.assertEqual(['first'], [record['message'] for record in first['logs']])
        self.assertEqual(['second'], [record['message'] for record in second['logs']])
        self.assertEqual('ERROR', second['logs'][0]['level'])


if __name__ == "__main__":
    unittest.main()
