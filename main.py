#!/usr/bin/env python3
"""
qBittorrent Load Balancer
监控torrent文件并智能分配到多个qBittorrent实例
"""

import json
import os
import ipaddress
import tempfile
import time
import threading
import logging
import requests
from collections import deque
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import qbittorrentapi
from webhook_server import WebhookServer
from telegram_notifier import TelegramNotifier


# 配置常量
DEFAULT_CONFIG_FILE = "config.json"

# 时间间隔常量（秒）
DEFAULT_SLEEP_TIME = 1
TASK_PROCESSOR_SLEEP = 1
STATUS_REFRESH_INTERVAL = 6
ERROR_RETRY_SLEEP = 5
RECONNECT_INTERVAL = 180
CONNECTION_TIMEOUT = 10
STATUS_REFRESH_AFTER_ADD_DELAY = 1
DAILY_TRAFFIC_SAVE_INTERVAL = 60
DAILY_TRAFFIC_STATE_FILENAME = 'dashboard_traffic_state.json'

# 网络和存储常量
BYTES_TO_KB = 1024
KIB_PER_MIB = 1024
BYTES_TO_GB = 1024 ** 3
BYTES_TO_TB = 1024 ** 4
MAX_RECONNECT_ATTEMPTS = 1

WAITING_DOWNLOAD_STATES = {'stalledDL', 'queuedDL', 'metaDL'}

# 支持的排序键（所有均为小值优先）
SUPPORTED_SORT_KEYS = {
    'upload_speed': '上传速度',
    'download_speed': '下载速度',
    'upload_download_speed': '上传速度+下载速度',
    'active_downloads': '活跃下载数',
    'total_downloads': '全部下载数'
}
DEFAULT_PRIMARY_SORT_KEY = 'upload_speed'
UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB = 500.0
UPLOAD_DOWNLOAD_SORT_UPLOAD_WEIGHT = 0.6
UPLOAD_DOWNLOAD_SORT_DOWNLOAD_WEIGHT = 0.4

# 创建一个简单的logger，避免在初始化之前输出日志
logger = logging.getLogger(__name__)


class DashboardLogHandler(logging.Handler):
    """Thread-safe bounded log buffer for incremental dashboard reads."""

    def __init__(self, capacity: int = 1000):
        super().__init__(logging.DEBUG)
        self.records = deque(maxlen=max(100, min(capacity, 5000)))
        self.records_lock = threading.Lock()
        self.sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                message = f"{message}\n{formatter.formatException(record.exc_info)}"
            with self.records_lock:
                self.sequence += 1
                self.records.append({
                    'id': self.sequence,
                    'timestamp': datetime.fromtimestamp(record.created).astimezone().isoformat(),
                    'level': record.levelname,
                    'logger': record.name,
                    'message': message,
                })
        except Exception:
            self.handleError(record)

    def read_after(self, after: int = 0, limit: int = 500) -> dict:
        limit = max(1, min(limit, 1000))
        with self.records_lock:
            matching = [item for item in self.records if item['id'] > after]
            return {'logs': matching[-limit:], 'cursor': self.sequence}

def setup_logging(log_dir=None):
    """设置日志配置，同时输出到控制台和文件"""
    # 初始化logger
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    
    app_logger = logging.getLogger(__name__)
    app_logger.setLevel(logging.DEBUG)
    app_logger.handlers.clear()
    
    # 设置基础格式
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # 添加控制台处理器
    _add_console_handler(app_logger, formatter)
    
    # 添加文件处理器（如果指定了日志目录）
    if log_dir:
        _add_file_handlers(app_logger, formatter, log_dir)
    
    return app_logger


def _add_console_handler(logger, formatter):
    """添加控制台日志处理器"""
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def _add_file_handlers(logger, formatter, log_dir):
    """添加文件日志处理器"""
    try:
        from logging.handlers import TimedRotatingFileHandler
        
        # 创建日志目录
        os.makedirs(log_dir, exist_ok=True)
        
        # 主日志文件
        main_log_path = os.path.join(log_dir, 'qbittorrent_loadbalancer.log')
        file_handler = _create_rotating_handler(main_log_path, logging.DEBUG, formatter)
        logger.addHandler(file_handler)
        
        # 错误日志文件
        error_log_path = os.path.join(log_dir, 'qbittorrent_error.log')
        error_handler = _create_rotating_handler(error_log_path, logging.ERROR, formatter)
        logger.addHandler(error_handler)
        
        logger.info(f"日志文件将保存到：{log_dir}")
        
    except Exception as e:
        print(f"警告: 无法设置文件日志: {e}")


def _create_rotating_handler(filename, level, formatter):
    """创建按日期轮转的日志处理器"""
    from logging.handlers import TimedRotatingFileHandler
    
    handler = TimedRotatingFileHandler(
        filename=filename,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _format_speed_rate(speed_kib_per_second: float) -> str:
    """格式化 KiB/s 速率，达到 1 MiB/s 时显示为 MiB/s。"""
    if speed_kib_per_second >= KIB_PER_MIB:
        return f"{speed_kib_per_second / KIB_PER_MIB:.1f}MB/s"
    return f"{speed_kib_per_second:.1f}KB/s"


@dataclass
class InstanceInfo:
    """qBittorrent实例信息"""
    name: str
    url: str
    username: str
    password: str
    client: Optional[qbittorrentapi.Client] = None
    is_connected: bool = False
    upload_speed: float = 0.0  # KB/s
    download_speed: float = 0.0  # KB/s
    active_downloads: int = 0
    waiting_downloads_count: int = 0
    free_space: int = 0  # bytes
    new_tasks_count: int = 0  # 新分配的任务数
    total_added_tasks_count: int = 0  # 已添加的总任务计数
    success_metrics_count: int = 0  # 成功获取统计信息的次数
    traffic_out: int = 0  # 出站流量 (bytes)
    traffic_limit: int = 0  # 流量限制 (bytes)
    traffic_check_url: str = ""  # 流量检查URL
    total_uploaded_bytes: int = 0  # qBittorrent累计上传流量
    total_downloaded_bytes: int = 0  # qBittorrent累计下载流量
    today_uploaded_bytes: int = 0  # 当日上传流量
    today_downloaded_bytes: int = 0  # 当日下载流量
    reserved_space: int = 0  # 需要保留的空闲空间 (bytes)
    last_update: datetime = field(default_factory=datetime.now)
    is_reconnecting: bool = False  # 是否正在重连中
    tracker_stats: Dict[str, dict] = field(default_factory=dict)


@dataclass
class PendingTorrent:
    """待处理的torrent"""
    download_url: str
    release_name: str
    category: Optional[str] = None
    failure_notified: bool = False


class QBittorrentLoadBalancer:
    """qBittorrent负载均衡器"""
    
    def __init__(self, config_file: str = DEFAULT_CONFIG_FILE):
        self.config_file = os.path.abspath(config_file)
        self.config = self._load_config(config_file)        
        self.instances: List[InstanceInfo] = []
        self.pending_torrents: List[PendingTorrent] = []
        self.pending_torrents_lock = threading.Lock()
        self.instances_lock = threading.Lock()
        self.status_refresh_event = threading.Event()
        self.config_lock = threading.Lock()
        history_points = int(self.config.get('dashboard', {}).get('history_points', 120))
        self.metrics_history = deque(maxlen=max(10, min(history_points, 600)))
        self.metrics_history_lock = threading.Lock()
        self.daily_traffic_lock = threading.Lock()
        self.daily_traffic_last_saved = 0.0
        
        # 重新配置日志（支持文件输出）
        self._setup_logging()
        self.daily_traffic_state = self._load_daily_traffic_state()
        self.telegram_notifier = TelegramNotifier(self.config)
        
        # 初始化webhook服务器
        self.webhook_server: Optional[WebhookServer] = None
        
        self._setup_environment()
        
    def _setup_logging(self) -> None:
        """根据配置设置日志"""
        global logger
        
        # 从配置中获取日志目录，默认为 /app/logs（Docker环境）或 ./logs（本地环境）
        log_dir = self.config.get('log_dir')
        if log_dir is None:
            # 自动检测环境
            if os.path.exists('/app'):  # Docker环境
                log_dir = '/app/logs'
            else:  # 本地环境
                log_dir = './logs'
        
        self.log_dir = os.path.abspath(log_dir)
        self.daily_traffic_state_file = os.path.join(self.log_dir, DAILY_TRAFFIC_STATE_FILENAME)
        logger = setup_logging(self.log_dir)
        log_capacity = int(self.config.get('dashboard', {}).get('log_limit', 1000))
        self.dashboard_log_handler = DashboardLogHandler(log_capacity)
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self.dashboard_log_handler)

    @staticmethod
    def _empty_daily_traffic_state(current: datetime) -> dict:
        return {'date': current.date().isoformat(), 'instances': {}}

    def _dashboard_timezone_name(self) -> str:
        dashboard = getattr(self, 'config', {}).get('dashboard', {})
        return str(dashboard.get('timezone', 'Asia/Shanghai') or 'Asia/Shanghai')

    def _dashboard_now(self) -> datetime:
        try:
            timezone = ZoneInfo(self._dashboard_timezone_name())
        except ZoneInfoNotFoundError:
            timezone = datetime_timezone(timedelta(hours=8), name='UTC+08:00')
        return datetime.now(timezone)

    def _load_daily_traffic_state(self) -> dict:
        current = self._dashboard_now()
        default = self._empty_daily_traffic_state(current)
        try:
            with open(self.daily_traffic_state_file, 'r', encoding='utf-8') as handle:
                state = json.load(handle)
            if state.get('date') != default['date'] or not isinstance(state.get('instances'), dict):
                return default
            return state
        except FileNotFoundError:
            return default
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("读取Dashboard每日流量状态失败，将从当前值开始统计：%s", exc)
            return default

    def _save_daily_traffic_state_locked(self, force: bool = False) -> None:
        """Persist daily counters atomically. Caller must hold daily_traffic_lock."""
        if not getattr(self, 'daily_traffic_state_file', None):
            return
        current_monotonic = time.monotonic()
        if not force and current_monotonic - self.daily_traffic_last_saved < DAILY_TRAFFIC_SAVE_INTERVAL:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix='traffic-', suffix='.json', dir=self.log_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(self.daily_traffic_state, handle, ensure_ascii=False, separators=(',', ':'))
                handle.write('\n')
            os.replace(temporary_path, self.daily_traffic_state_file)
            self.daily_traffic_last_saved = current_monotonic
        except Exception:
            self.daily_traffic_last_saved = current_monotonic
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise

    @staticmethod
    def _counter_delta(current: int, previous: Optional[int]) -> int:
        if previous is None:
            return 0
        return current - previous if current >= previous else current

    @staticmethod
    def _tracker_name(torrent) -> str:
        tracker_url = str(getattr(torrent, 'tracker', '') or '').strip()
        tracker = urlparse(tracker_url).hostname if tracker_url else None
        return (tracker or tracker_url or '无 Tracker').lower()

    def _update_daily_traffic(self, instance: InstanceInfo, torrents: dict, current: Optional[datetime] = None) -> None:
        current = current or self._dashboard_now()
        if not hasattr(self, 'daily_traffic_lock'):
            self.daily_traffic_lock = threading.Lock()
        if not hasattr(self, 'daily_traffic_last_saved'):
            self.daily_traffic_last_saved = 0.0
        if not hasattr(self, 'daily_traffic_state'):
            self.daily_traffic_state = self._empty_daily_traffic_state(current)

        with self.daily_traffic_lock:
            rolled_over = self.daily_traffic_state.get('date') != current.date().isoformat()
            if rolled_over:
                self.daily_traffic_state = self._empty_daily_traffic_state(current)

            instances = self.daily_traffic_state.setdefault('instances', {})
            state = instances.setdefault(instance.name, {
                'today_uploaded_bytes': 0,
                'today_downloaded_bytes': 0,
                'trackers': {},
                'torrents': {},
            })

            previous_uploaded = state.get('last_uploaded_bytes')
            previous_downloaded = state.get('last_downloaded_bytes')
            state['today_uploaded_bytes'] = int(state.get('today_uploaded_bytes', 0)) + self._counter_delta(
                instance.total_uploaded_bytes, previous_uploaded
            )
            state['today_downloaded_bytes'] = int(state.get('today_downloaded_bytes', 0)) + self._counter_delta(
                instance.total_downloaded_bytes, previous_downloaded
            )
            state['last_uploaded_bytes'] = instance.total_uploaded_bytes
            state['last_downloaded_bytes'] = instance.total_downloaded_bytes

            tracker_daily = state.setdefault('trackers', {})
            torrent_counters = state.setdefault('torrents', {})
            midnight_timestamp = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for torrent_hash, torrent in torrents.items():
                tracker = self._tracker_name(torrent)
                uploaded = max(0, int(getattr(torrent, 'uploaded', 0) or 0))
                downloaded = max(0, int(getattr(torrent, 'downloaded', 0) or 0))
                previous = torrent_counters.get(str(torrent_hash))
                if previous is None:
                    try:
                        added_today = float(getattr(torrent, 'added_on', 0) or 0) >= midnight_timestamp
                    except (TypeError, ValueError):
                        added_today = False
                    upload_delta = uploaded if added_today else 0
                    download_delta = downloaded if added_today else 0
                else:
                    upload_delta = self._counter_delta(uploaded, int(previous.get('uploaded_bytes', 0)))
                    download_delta = self._counter_delta(downloaded, int(previous.get('downloaded_bytes', 0)))
                totals = tracker_daily.setdefault(tracker, {'uploaded_bytes': 0, 'downloaded_bytes': 0})
                totals['uploaded_bytes'] += upload_delta
                totals['downloaded_bytes'] += download_delta
                torrent_counters[str(torrent_hash)] = {
                    'tracker': tracker,
                    'uploaded_bytes': uploaded,
                    'downloaded_bytes': downloaded,
                }

            instance.today_uploaded_bytes = int(state['today_uploaded_bytes'])
            instance.today_downloaded_bytes = int(state['today_downloaded_bytes'])
            for tracker, stats in instance.tracker_stats.items():
                today = tracker_daily.get(tracker, {})
                stats['today_uploaded_bytes'] = int(today.get('uploaded_bytes', 0))
                stats['today_downloaded_bytes'] = int(today.get('downloaded_bytes', 0))

            try:
                self._save_daily_traffic_state_locked(force=rolled_over)
            except OSError as exc:
                logger.warning("保存Dashboard每日流量状态失败：%s", exc)
        
    def _setup_environment(self) -> None:
        """设置运行环境"""
        # 验证配置
        self._validate_config()
        # 设置配置默认值和验证
        self._set_config_defaults()
        # 初始化qBittorrent实例
        self._init_instances()
        
        # 启动webhook服务器
        self._start_webhook_server()
        
    def _validate_config(self) -> None:
        """验证配置文件的有效性"""
        # 验证primary_sort_key配置
        primary_sort_key = self.config.get('primary_sort_key', DEFAULT_PRIMARY_SORT_KEY)
        if primary_sort_key not in SUPPORTED_SORT_KEYS:
            logger.warning(f"不支持的排序键：{primary_sort_key}，使用默认值：{DEFAULT_PRIMARY_SORT_KEY}")
            self.config['primary_sort_key'] = DEFAULT_PRIMARY_SORT_KEY
        else:
            logger.info(f"使用排序策略：主要因素={SUPPORTED_SORT_KEYS[primary_sort_key]}，次要因素=累计添加任务数，第三因素=空闲空间")

    def _load_config(self, config_file: str) -> dict:
        """加载配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"配置文件未找到：{config_file}")
            raise
        except json.JSONDecodeError:
            logger.error(f"配置文件格式错误：{config_file}")
            raise
    
    def _set_config_defaults(self) -> None:
        """设置配置默认值和验证"""
        self.config.setdefault('max_new_tasks_per_instance', 2)
        self.config.setdefault('webhook_ip_whitelist', [])
        self.config.setdefault('telegram', {'enabled': False})
        dashboard = self.config.setdefault('dashboard', {'enabled': False})
        dashboard.setdefault('timezone', 'Asia/Shanghai')
        dashboard.setdefault('timezone_configured', False)
        try:
            ZoneInfo(str(dashboard['timezone']))
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Dashboard时区无效，恢复为Asia/Shanghai并要求重新选择")
            dashboard['timezone'] = 'Asia/Shanghai'
            dashboard['timezone_configured'] = False
        logger.info(f"状态更新间隔配置：{STATUS_REFRESH_INTERVAL}秒")

    def _init_instances(self) -> None:
        """初始化qBittorrent实例连接"""
        for instance_config in self.config['qbittorrent_instances']:
            instance = self._create_instance_from_config(instance_config)
            self._connect_instance(instance)
            self.instances.append(instance)
            
    def _create_instance_from_config(self, config: Dict[str, str]) -> InstanceInfo:
        """根据配置创建实例信息对象"""
        # 安全地转换流量限制值（从MB转换为字节）
        try:
            traffic_limit_mb = config.get('traffic_limit', 0.0)
            traffic_limit_bytes = int(float(traffic_limit_mb) * 1024 * 1024)  # MB转字节
        except (ValueError, TypeError) as e:
            logger.warning(f"实例 {config.get('name', 'Unknown')} 流量限制值转换失败：{e}，设置为0")
            traffic_limit_bytes = 0
        
        # 安全地转换保留空间值（从MB转换为字节）
        try:
            reserved_space_mb = config.get('reserved_space', 0)
            reserved_space_bytes = int(float(reserved_space_mb) * 1024 * 1024)  # MB转字节
        except (ValueError, TypeError) as e:
            logger.warning(f"实例 {config.get('name', 'Unknown')} 保留空间值转换失败：{e}，设置为0")
            reserved_space_bytes = 0
            
        return InstanceInfo(
            name=config['name'],
            url=config['url'],
            username=config['username'],
            password=config['password'],
            traffic_check_url=config.get('traffic_check_url', ''),
            traffic_limit=traffic_limit_bytes,
            reserved_space=reserved_space_bytes
        )
        
    def _connect_instance(self, instance: InstanceInfo) -> None:
        """连接到qBittorrent实例"""
        try:
            connection_timeout = self.config.get('connection_timeout', CONNECTION_TIMEOUT)
            client = qbittorrentapi.Client(
                host=instance.url,
                username=instance.username,
                password=instance.password,
                REQUESTS_ARGS={'timeout': connection_timeout}
            )
            client.auth_log_in()
            instance.client = client
            instance.is_connected = True
            logger.info(f"成功连接到实例：{instance.name}")
        except Exception as e:
            logger.error(f"连接实例失败：{instance.name}，错误：{e}")
            instance.is_connected = False
            # 记录连接失败的时间，用于后续重连判断
            instance.last_update = datetime.now()
            self._notify(f"[实例离线] {instance.name}\n初始连接失败：{e}")
            
    def _attempt_reconnect(self, instance: InstanceInfo) -> bool:
        """尝试重新连接到实例"""
        logger.info(f"尝试重新连接到实例：{instance.name}")
        
        max_attempts = self.config.get('max_reconnect_attempts', MAX_RECONNECT_ATTEMPTS)
        connection_timeout = self.config.get('connection_timeout', CONNECTION_TIMEOUT)
        
        for attempt in range(max_attempts):
            try:
                client = qbittorrentapi.Client(
                    host=instance.url,
                    username=instance.username,
                    password=instance.password,
                    REQUESTS_ARGS={'timeout': connection_timeout}
                )
                
                # 设置连接超时并尝试登录
                client.auth_log_in()
                
                # 更新实例状态需要在锁内进行
                with self.instances_lock:
                    instance.client = client
                    instance.is_connected = True
                    instance.is_reconnecting = False
                    
                logger.info(f"重新连接成功：{instance.name}（尝试 {attempt + 1}/{max_attempts}）")
                self._notify(f"[实例恢复] {instance.name}\n已重新连接")
                return True
                
            except Exception as e:
                logger.warning(f"重连尝试 {attempt + 1}/{max_attempts} 失败：{instance.name}，错误：{e}")
                if attempt < max_attempts - 1:
                    time.sleep(2)  # 每次重连尝试间等待2秒
                    
        logger.error(f"重连彻底失败：{instance.name}")
        self._notify(f"[重连失败] {instance.name}\n已尝试 {max_attempts} 次")
        
        # 更新失败时间需要在锁内进行
        with self.instances_lock:
            instance.last_update = datetime.now()
            instance.is_reconnecting = False
            
        return False
        
    def _async_reconnect_instance(self, instance: InstanceInfo) -> None:
        """异步重连单个实例（在独立线程中执行）"""
        try:
            self._attempt_reconnect(instance)
        except Exception as e:
            logger.error(f"异步重连过程中发生异常：{instance.name}，错误：{e}")
            with self.instances_lock:
                instance.last_update = datetime.now()
                instance.is_reconnecting = False
        
    def _check_and_schedule_reconnects(self) -> None:
        """检查断开的实例并调度重连（非阻塞）"""
        current_time = datetime.now()
        reconnect_interval = self.config.get('reconnect_interval', RECONNECT_INTERVAL)
        
        instances_to_reconnect = []
        
        with self.instances_lock:
            for instance in self.instances:
                # 只处理未连接且未在重连中的实例
                if not instance.is_connected and not instance.is_reconnecting:
                    # 检查是否到了重连时间
                    time_since_last_attempt = (current_time - instance.last_update).total_seconds()
                    if time_since_last_attempt >= reconnect_interval:
                        instances_to_reconnect.append(instance)
                        # 标记为正在重连，防止重复调度
                        instance.is_reconnecting = True
                        instance.last_update = current_time
                        
        # 在锁外启动重连线程，避免阻塞
        for instance in instances_to_reconnect:
            logger.info(f"开始重连任务：{instance.name}")
            threading.Thread(
                target=self._async_reconnect_instance,
                args=(instance,),
                daemon=True,
                name=f"reconnect-{instance.name}"
            ).start()
        
    def _start_webhook_server(self) -> None:
        """启动webhook服务器"""
        try:
            self.webhook_server = WebhookServer(self, self.config)
            self.webhook_server.start()
            logger.info("Webhook服务器已启动")
        except Exception as e:
            logger.error(f"启动webhook服务器失败: {e}")
            raise
            
    def add_pending_torrent(self, download_url: str, release_name: str, category: Optional[str] = None) -> None:
        """添加待处理的torrent"""
        if not download_url:
            logger.error("必须提供download_url")
            return
            
        if not release_name:
            logger.error("必须提供release_name")
            return
        
        try:
            with self.pending_torrents_lock:
                # 检查是否已存在（使用URL作为唯一标识）
                exists = any(t.download_url == download_url for t in self.pending_torrents)
                
                if not exists:
                    torrent = PendingTorrent(
                        download_url=download_url,
                        release_name=release_name,
                        category=category
                    )
                    self.pending_torrents.append(torrent)
                    logger.info(f"添加待处理种子：{release_name}")
                else:
                    logger.debug(f"种子已在待处理列表中：{release_name}")
                    
        except Exception as e:
            logger.error(f"添加种子失败：{release_name}，错误：{e}")

    def _notify(self, message: str) -> None:
        """Queue a Telegram notification when configured."""
        notifier = getattr(self, 'telegram_notifier', None)
        if notifier:
            notifier.send(message)

    def get_dashboard_snapshot(self) -> dict:
        """Return a JSON-safe snapshot for the dashboard."""
        with self.instances_lock:
            instances = [
                {
                    'name': instance.name,
                    'connected': instance.is_connected,
                    'reconnecting': instance.is_reconnecting,
                    'upload_speed_kib': round(instance.upload_speed, 1),
                    'download_speed_kib': round(instance.download_speed, 1),
                    'active_downloads': instance.active_downloads,
                    'waiting_downloads': instance.waiting_downloads_count,
                    'free_space_gib': round(instance.free_space / BYTES_TO_GB, 1),
                    'reserved_space_gib': round(instance.reserved_space / BYTES_TO_GB, 1),
                    'traffic_out_gib': round(instance.traffic_out / BYTES_TO_GB, 2),
                    'traffic_limit_gib': round(instance.traffic_limit / BYTES_TO_GB, 2),
                    'today_uploaded_bytes': instance.today_uploaded_bytes,
                    'today_downloaded_bytes': instance.today_downloaded_bytes,
                    'today_traffic_bytes': instance.today_uploaded_bytes + instance.today_downloaded_bytes,
                    'total_uploaded_bytes': instance.total_uploaded_bytes,
                    'total_downloaded_bytes': instance.total_downloaded_bytes,
                    'total_traffic_bytes': instance.total_uploaded_bytes + instance.total_downloaded_bytes,
                    'total_added_tasks': instance.total_added_tasks_count,
                    'last_update': instance.last_update.isoformat(),
                }
                for instance in self.instances
            ]
            tracker_totals = {}
            for instance in self.instances:
                for tracker, stats in instance.tracker_stats.items():
                    total = tracker_totals.setdefault(tracker, {
                        'tracker': tracker,
                        'torrent_count': 0,
                        'active_downloads': 0,
                        'upload_speed_kib': 0.0,
                        'download_speed_kib': 0.0,
                        'uploaded_bytes': 0,
                        'downloaded_bytes': 0,
                        'today_uploaded_bytes': 0,
                        'today_downloaded_bytes': 0,
                        'instances': [],
                        'instance_torrent_counts': {},
                    })
                    total['torrent_count'] += stats['torrent_count']
                    total['active_downloads'] += stats['active_downloads']
                    total['upload_speed_kib'] += stats['upload_speed_kib']
                    total['download_speed_kib'] += stats['download_speed_kib']
                    total['uploaded_bytes'] += stats['uploaded_bytes']
                    total['downloaded_bytes'] += stats['downloaded_bytes']
                    total['today_uploaded_bytes'] += stats.get('today_uploaded_bytes', 0)
                    total['today_downloaded_bytes'] += stats.get('today_downloaded_bytes', 0)
                    total['instances'].append(instance.name)
                    total['instance_torrent_counts'][instance.name] = stats['torrent_count']
            tracker_stats = sorted(
                tracker_totals.values(),
                key=lambda item: (-item['torrent_count'], item['tracker']),
            )
            for item in tracker_stats:
                item['upload_speed_kib'] = round(item['upload_speed_kib'], 1)
                item['download_speed_kib'] = round(item['download_speed_kib'], 1)
                item['instance_torrent_counts'] = [
                    {'name': name, 'torrent_count': count}
                    for name, count in sorted(item['instance_torrent_counts'].items())
                ]
            traffic_totals = {
                'uploaded_bytes': sum(instance.total_uploaded_bytes for instance in self.instances),
                'downloaded_bytes': sum(instance.total_downloaded_bytes for instance in self.instances),
                'today_uploaded_bytes': sum(instance.today_uploaded_bytes for instance in self.instances),
                'today_downloaded_bytes': sum(instance.today_downloaded_bytes for instance in self.instances),
            }
        history_lock = getattr(self, 'metrics_history_lock', None)
        if history_lock:
            with history_lock:
                metrics_history = list(self.metrics_history)
        else:
            metrics_history = []
        with self.pending_torrents_lock:
            pending_count = len(self.pending_torrents)
        with self.config_lock:
            whitelist = list(self.config.get('webhook_ip_whitelist', []))
            telegram = self.config.get('telegram', {})
            dashboard = self.config.get('dashboard', {})
            active_notifier = getattr(self, 'telegram_notifier', None)
            telegram_status = {
                'enabled': bool(active_notifier and active_notifier.enabled),
                'chat_id': str(telegram.get('chat_id', '')),
                'bot_token_configured': bool(telegram.get('bot_token')),
                'timeout': telegram.get('timeout', 10),
            }
            configured_instances = [
                {
                    'name': item.get('name', ''),
                    'url': item.get('url', ''),
                    'username': item.get('username', ''),
                    'has_password': bool(item.get('password')),
                    'traffic_check_url': item.get('traffic_check_url', ''),
                    'traffic_limit': item.get('traffic_limit', 0),
                    'reserved_space': item.get('reserved_space', 0),
                }
                for item in self.config.get('qbittorrent_instances', [])
            ]
        return {
            'instances': instances,
            'configured_instances': configured_instances,
            'pending_count': pending_count,
            'whitelist': whitelist,
            'telegram': telegram_status,
            'metrics_history': metrics_history,
            'tracker_stats': tracker_stats,
            'traffic_totals': traffic_totals,
            'dashboard_timezone': {
                'name': str(dashboard.get('timezone', 'Asia/Shanghai')),
                'configured': bool(dashboard.get('timezone_configured', False)),
                'date': self._dashboard_now().date().isoformat(),
            },
            'sort_key': self.config.get('primary_sort_key', DEFAULT_PRIMARY_SORT_KEY),
            'updated_at': self._dashboard_now().isoformat(),
        }

    def is_webhook_ip_allowed(self, address: str) -> bool:
        """Check an address against the configured IP/CIDR whitelist."""
        try:
            client_ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        with self.config_lock:
            entries = list(self.config.get('webhook_ip_whitelist', []))
        if not entries:
            return True
        for entry in entries:
            try:
                if client_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                logger.warning("忽略无效的Webhook白名单项：%s", entry)
        return False

    def add_webhook_whitelist_entry(self, entry: str) -> str:
        """Validate, normalize and persist an IP/CIDR whitelist entry."""
        network = ipaddress.ip_network(entry.strip(), strict=False)
        normalized = str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)
        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            entries = candidate.setdefault('webhook_ip_whitelist', [])
            if normalized not in entries:
                entries.append(normalized)
                self._replace_config_locked(candidate)
        return normalized

    def remove_webhook_whitelist_entry(self, entry: str) -> bool:
        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            entries = candidate.setdefault('webhook_ip_whitelist', [])
            if entry not in entries:
                return False
            entries.remove(entry)
            self._replace_config_locked(candidate)
            return True

    def _replace_config_locked(self, candidate: dict) -> None:
        """Persist and activate candidate config. Caller must hold config_lock."""
        directory = os.path.dirname(self.config_file) or '.'
        fd, temporary_path = tempfile.mkstemp(prefix='config-', suffix='.json', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(candidate, handle, ensure_ascii=False, indent=4)
                handle.write('\n')
            try:
                os.replace(temporary_path, self.config_file)
            except OSError:
                # Docker bind-mounted files cannot always be replaced by rename.
                with open(self.config_file, 'w', encoding='utf-8') as handle:
                    json.dump(candidate, handle, ensure_ascii=False, indent=4)
                    handle.write('\n')
                os.unlink(temporary_path)
            self.config.clear()
            self.config.update(candidate)
        except Exception:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise

    def upsert_qbittorrent_instance(self, payload: dict, original_name: str = '') -> dict:
        """Create or update an instance, persist it and apply it immediately."""
        required = ('name', 'url', 'username')
        values = {key: str(payload.get(key, '')).strip() for key in required}
        if any(not values[key] for key in required):
            raise ValueError('name, url and username are required')

        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            configs = candidate.setdefault('qbittorrent_instances', [])
            existing_index = next(
                (index for index, item in enumerate(configs) if item.get('name') == original_name),
                None,
            ) if original_name else None
            if any(
                item.get('name') == values['name'] and index != existing_index
                for index, item in enumerate(configs)
            ):
                raise ValueError('instance name already exists')
            previous = configs[existing_index] if existing_index is not None else {}
            password = str(payload.get('password', '')) or str(previous.get('password', ''))
            if not password:
                raise ValueError('password is required for a new instance')
            instance_config = {
                'name': values['name'],
                'url': values['url'],
                'username': values['username'],
                'password': password,
                'traffic_check_url': str(payload.get('traffic_check_url', '')).strip(),
                'traffic_limit': self._non_negative_number(payload.get('traffic_limit', 0), 'traffic_limit'),
                'reserved_space': self._non_negative_number(payload.get('reserved_space', 0), 'reserved_space'),
            }
            new_instance = self._create_instance_from_config(instance_config)
            self._connect_instance(new_instance)
            if existing_index is None:
                configs.append(instance_config)
            else:
                configs[existing_index] = instance_config
            self._replace_config_locked(candidate)

        with self.instances_lock:
            runtime_index = next(
                (index for index, item in enumerate(self.instances) if item.name == original_name),
                None,
            ) if original_name else None
            if runtime_index is None:
                self.instances.append(new_instance)
            else:
                new_instance.total_added_tasks_count = self.instances[runtime_index].total_added_tasks_count
                self.instances[runtime_index] = new_instance
        return {'name': new_instance.name, 'connected': new_instance.is_connected}

    def delete_qbittorrent_instance(self, name: str) -> bool:
        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            configs = candidate.setdefault('qbittorrent_instances', [])
            remaining = [item for item in configs if item.get('name') != name]
            if len(remaining) == len(configs):
                return False
            candidate['qbittorrent_instances'] = remaining
            self._replace_config_locked(candidate)
        with self.instances_lock:
            self.instances[:] = [item for item in self.instances if item.name != name]
        return True

    def clone_qbittorrent_instance(self, name: str) -> dict:
        """Clone an instance without exposing its password to the dashboard."""
        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            configs = candidate.setdefault('qbittorrent_instances', [])
            source = next((item for item in configs if item.get('name') == name), None)
            if source is None:
                raise ValueError('instance not found')
            existing_names = {item.get('name') for item in configs}
            clone_name = f'{name}-copy'
            suffix = 2
            while clone_name in existing_names:
                clone_name = f'{name}-copy-{suffix}'
                suffix += 1
            clone_config = json.loads(json.dumps(source))
            clone_config['name'] = clone_name
            clone_instance = self._create_instance_from_config(clone_config)
            self._connect_instance(clone_instance)
            configs.append(clone_config)
            self._replace_config_locked(candidate)
        with self.instances_lock:
            self.instances.append(clone_instance)
        return {'name': clone_name, 'connected': clone_instance.is_connected}

    def get_dashboard_logs(self, after: int = 0, limit: int = 500) -> dict:
        handler = getattr(self, 'dashboard_log_handler', None)
        return handler.read_after(after, limit) if handler else {'logs': [], 'cursor': 0}

    def import_config(self, imported: dict) -> dict:
        """Import old or current config and hot-apply qBittorrent instances."""
        if not isinstance(imported, dict):
            raise ValueError('config must be a JSON object')
        configs = imported.get('qbittorrent_instances')
        if not isinstance(configs, list):
            raise ValueError('qbittorrent_instances must be a list')
        normalized_configs = []
        names = set()
        for item in configs:
            if not isinstance(item, dict):
                raise ValueError('each qBittorrent instance must be an object')
            for key in ('name', 'url', 'username', 'password'):
                if not str(item.get(key, '')).strip():
                    raise ValueError(f'instance {key} is required')
            if item['name'] in names:
                raise ValueError('instance names must be unique')
            names.add(item['name'])
            normalized_configs.append(dict(item))

        with self.config_lock:
            candidate = json.loads(json.dumps(imported))
            for key in ('dashboard', 'telegram', 'webhook_ip_whitelist'):
                if key not in candidate and key in self.config:
                    candidate[key] = json.loads(json.dumps(self.config[key]))
            candidate.setdefault('max_new_tasks_per_instance', 2)
            candidate.setdefault('webhook_port', 50000)
            candidate.setdefault('webhook_path', '/webhook')
            candidate.setdefault('primary_sort_key', DEFAULT_PRIMARY_SORT_KEY)
            if candidate['primary_sort_key'] not in SUPPORTED_SORT_KEYS:
                candidate['primary_sort_key'] = DEFAULT_PRIMARY_SORT_KEY
            new_instances = [self._create_instance_from_config(item) for item in normalized_configs]
            for instance in new_instances:
                self._connect_instance(instance)
            self._replace_config_locked(candidate)
        with self.instances_lock:
            self.instances[:] = new_instances
        previous_notifier = getattr(self, 'telegram_notifier', None)
        if previous_notifier:
            previous_notifier.stop()
        self.telegram_notifier = TelegramNotifier(self.config)
        return {
            'instances': len(new_instances),
            'connected': sum(1 for item in new_instances if item.is_connected),
            'restart_required': True,
        }

    def update_telegram_config(self, payload: dict) -> dict:
        """Persist Telegram settings and replace the notifier immediately."""
        raw_enabled = payload.get('enabled', False)
        enabled = raw_enabled is True or str(raw_enabled).lower() in {'1', 'true', 'yes', 'on'}
        try:
            timeout = float(payload.get('timeout', 10))
        except (TypeError, ValueError):
            raise ValueError('timeout must be a number')
        if timeout < 1 or timeout > 60:
            raise ValueError('timeout must be between 1 and 60 seconds')

        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            current = candidate.get('telegram', {})
            bot_token = str(payload.get('bot_token', '')).strip() or str(current.get('bot_token', '')).strip()
            chat_id = str(payload.get('chat_id', '')).strip() or str(current.get('chat_id', '')).strip()
            if enabled and (not bot_token or not chat_id):
                raise ValueError('bot_token and chat_id are required when Telegram is enabled')
            candidate['telegram'] = {
                'enabled': enabled,
                'bot_token': bot_token,
                'chat_id': chat_id,
                'timeout': timeout,
            }
            self._replace_config_locked(candidate)

        previous = getattr(self, 'telegram_notifier', None)
        if previous:
            previous.stop()
        self.telegram_notifier = TelegramNotifier(self.config)
        return {
            'enabled': self.telegram_notifier.enabled,
            'chat_id': chat_id,
            'bot_token_configured': bool(bot_token),
            'timeout': timeout,
        }

    def update_dashboard_timezone(self, timezone_name: str) -> dict:
        """Persist the Dashboard timezone and reset today's traffic baseline."""
        timezone_name = str(timezone_name or '').strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('invalid timezone')

        with self.config_lock:
            candidate = json.loads(json.dumps(self.config))
            dashboard = candidate.setdefault('dashboard', {})
            dashboard['timezone'] = timezone_name
            dashboard['timezone_configured'] = True
            self._replace_config_locked(candidate)

        current = datetime.now(timezone)
        with self.instances_lock:
            with self.daily_traffic_lock:
                self.daily_traffic_state = self._empty_daily_traffic_state(current)
                for instance in self.instances:
                    instance.today_uploaded_bytes = 0
                    instance.today_downloaded_bytes = 0
                    for stats in instance.tracker_stats.values():
                        stats['today_uploaded_bytes'] = 0
                        stats['today_downloaded_bytes'] = 0
                self._save_daily_traffic_state_locked(force=True)
        return {'name': timezone_name, 'date': current.date().isoformat()}

    def send_telegram_test(self) -> bool:
        notifier = getattr(self, 'telegram_notifier', None)
        if not notifier:
            raise RuntimeError('Telegram通知器未初始化')
        notifier.test(
            f"[测试通知] qBittorrent Load Balancer\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return True

    @staticmethod
    def _non_negative_number(value, field_name: str):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{field_name} must be a number')
        if number < 0:
            raise ValueError(f'{field_name} must not be negative')
        return int(number) if number.is_integer() else number
            

                
    def _update_instance_status(self) -> None:
        """更新所有实例的状态信息"""
        with self.instances_lock:
            for instance in self.instances:
                if instance.is_connected:
                    self._update_single_instance(instance)
            upload_speed = sum(instance.upload_speed for instance in self.instances if instance.is_connected)
            download_speed = sum(instance.download_speed for instance in self.instances if instance.is_connected)
        history = getattr(self, 'metrics_history', None)
        history_lock = getattr(self, 'metrics_history_lock', None)
        if history is not None and history_lock:
            with history_lock:
                history.append({
                    'timestamp': self._dashboard_now().isoformat(),
                    'upload_speed_kib': round(upload_speed, 1),
                    'download_speed_kib': round(download_speed, 1),
                })
                    
    def _update_single_instance(self, instance: InstanceInfo) -> None:
        """更新单个实例的状态信息"""
        def _try_update_instance():
            """尝试更新实例状态的内部函数"""
            maindata = instance.client.sync_maindata()
            self._update_instance_metrics(instance, maindata)
        
        # 第一次尝试
        try:
            _try_update_instance()
            return
        except Exception as e:
            logger.warning(f"更新实例状态失败：{instance.name}，错误：{e}，等待5秒后重试")
            time.sleep(5)
        
        # 第二次尝试
        try:
            _try_update_instance()
            logger.info(f"实例 {instance.name} 重试成功")
        except Exception as e2:
            logger.error(f"重试后仍然失败：{instance.name}，错误：{e2}，标记为断开连接")
            instance.is_connected = False
            instance.last_update = datetime.now()
            self._notify(f"[实例离线] {instance.name}\n状态更新连续失败：{e2}")
                    
    def _update_instance_metrics(self, instance: InstanceInfo, maindata: dict) -> None:
        """使用sync/maindata的结果更新单个实例的状态信息"""
        server_state = maindata.get('server_state', {})
        
        # 从server_state获取全局统计信息和硬盘空间
        instance.upload_speed = server_state.get('up_info_speed', 0) / BYTES_TO_KB
        instance.download_speed = server_state.get('dl_info_speed', 0) / BYTES_TO_KB
        instance.total_uploaded_bytes = max(0, int(server_state.get('alltime_ul', 0) or 0))
        instance.total_downloaded_bytes = max(0, int(server_state.get('alltime_dl', 0) or 0))
        instance.free_space = server_state.get('free_space_on_disk', 0)
        
        # 从torrents信息计算活跃下载数
        torrents_by_hash = dict(maindata.get('torrents', {}))
        all_torrents = list(torrents_by_hash.values())
        instance.active_downloads = len([t for t in all_torrents if t.state == 'downloading'])
        instance.waiting_downloads_count = len([
            t for t in all_torrents if t.state in WAITING_DOWNLOAD_STATES
        ])
        instance.tracker_stats = self._aggregate_tracker_stats(all_torrents)
        self._update_daily_traffic(instance, torrents_by_hash)
        
        instance.last_update = datetime.now()
        instance.success_metrics_count += 1  # 成功获取统计信息，计数器加1
        
        # 每30次成功更新时检查一次流量信息
        if instance.success_metrics_count % 30 == 0:
            self._check_instance_traffic(instance)
        
        logger.debug(f"实例 {instance.name}：" 
                   f"上传={_format_speed_rate(instance.upload_speed)}，"
                   f"下载={_format_speed_rate(instance.download_speed)}，"
                   f"活跃下载={instance.active_downloads}，"
                   f"等待下载={instance.waiting_downloads_count}，"
                   f"空间={instance.free_space/BYTES_TO_GB:.1f}/{instance.reserved_space/BYTES_TO_GB:.1f}GB，"
                   f"更新={instance.success_metrics_count}，"
                   f"历史任务={instance.total_added_tasks_count}")

    @staticmethod
    def _aggregate_tracker_stats(torrents) -> Dict[str, dict]:
        """Aggregate live torrent counts and speeds by current tracker host."""
        trackers = {}
        for torrent in torrents:
            tracker = QBittorrentLoadBalancer._tracker_name(torrent)
            stats = trackers.setdefault(tracker, {
                'torrent_count': 0,
                'active_downloads': 0,
                'upload_speed_kib': 0.0,
                'download_speed_kib': 0.0,
                'uploaded_bytes': 0,
                'downloaded_bytes': 0,
                'today_uploaded_bytes': 0,
                'today_downloaded_bytes': 0,
            })
            stats['torrent_count'] += 1
            if getattr(torrent, 'state', '') == 'downloading':
                stats['active_downloads'] += 1
            stats['upload_speed_kib'] += float(getattr(torrent, 'upspeed', 0) or 0) / BYTES_TO_KB
            stats['download_speed_kib'] += float(getattr(torrent, 'dlspeed', 0) or 0) / BYTES_TO_KB
            stats['uploaded_bytes'] += max(0, int(getattr(torrent, 'uploaded', 0) or 0))
            stats['downloaded_bytes'] += max(0, int(getattr(torrent, 'downloaded', 0) or 0))
        return trackers


    def _check_instance_traffic(self, instance: InstanceInfo) -> None:
        """检查实例的流量信息"""
        if not instance.traffic_check_url:
            return
            
        try:
            response = requests.get(instance.traffic_check_url, timeout=5)
            response.raise_for_status()
            traffic_data = response.json()
            
            # 获取出站流量，从MB转换为字节
            try:
                traffic_out_mb = traffic_data.get('out', 0.0)
                instance.traffic_out = int(float(traffic_out_mb) * 1024 * 1024)  # MB转字节
                
                # 检查是否流量被限流
                traffic_throttled = traffic_data.get('trafficThrottled', False)
                if traffic_throttled:
                    instance.traffic_out = 9999 * BYTES_TO_TB  # 设置为极大值，确保在流量检查时被过滤
                    logger.warning(f"实例 {instance.name} 流量被限流，设置流量为极大值以避免被选择")
                    
            except (ValueError, TypeError) as e:
                logger.warning(f"实例 {instance.name} 流量数据转换失败：{e}，设置为0")
                instance.traffic_out = 0
            
            logger.debug(f"更新实例 {instance.name} 流量信息：出站流量={instance.traffic_out/BYTES_TO_GB:.2f}GB，限制={instance.traffic_limit/BYTES_TO_GB:.2f}GB")
            
        except Exception as e:
            logger.warning(f"获取实例 {instance.name} 流量信息失败：{e}")
            instance.traffic_out = 0
    
    def _is_traffic_within_limit(self, instance: InstanceInfo) -> bool:
        """检查实例的流量是否在限制范围内"""
        # 如果出站流量为0（未检查或检查失败），认为流量未超出
        if instance.traffic_out == 0:
            return True
        
        # 如果没有设置流量限制，认为流量未超出
        if instance.traffic_limit == 0:
            return True
            
        # 比较出站流量和流量限制
        within_limit = instance.traffic_out < instance.traffic_limit
        
        if not within_limit:
            logger.warning(f"实例 {instance.name} 流量超限：出站流量={instance.traffic_out/BYTES_TO_GB:.2f}GB，限制={instance.traffic_limit/BYTES_TO_GB:.2f}GB")
        
        return within_limit
    def _get_primary_sort_value(self, instance: InstanceInfo) -> float:
        """获取主要排序因素的值"""
        primary_sort_key = self.config.get('primary_sort_key', DEFAULT_PRIMARY_SORT_KEY)
        
        if primary_sort_key == 'upload_speed':
            return self._get_upload_speed_sort_value(instance)
        elif primary_sort_key == 'download_speed':
            return instance.download_speed
        elif primary_sort_key == 'upload_download_speed':
            return (
                self._get_upload_speed_sort_value(instance) * UPLOAD_DOWNLOAD_SORT_UPLOAD_WEIGHT
                + instance.download_speed * UPLOAD_DOWNLOAD_SORT_DOWNLOAD_WEIGHT
            )
        elif primary_sort_key == 'active_downloads':
            return float(instance.active_downloads)
        elif primary_sort_key == 'total_downloads':
            return instance.active_downloads + 0.5 * instance.waiting_downloads_count
        else:
            # 默认使用上传速度
            return self._get_upload_speed_sort_value(instance)

    def _get_upload_speed_sort_value(self, instance: InstanceInfo) -> float:
        """获取上传速度排序值，低速上传视为空闲。"""
        if instance.upload_speed < UPLOAD_SPEED_SORT_ZERO_THRESHOLD_KIB:
            return 0.0
        return instance.upload_speed
        
    def _select_best_instance(self) -> Optional[InstanceInfo]:
        """选择最佳的实例来分配新任务"""
        with self.instances_lock:
            available_instances = [
                instance for instance in self.instances 
                if instance.is_connected and 
                instance.new_tasks_count < self.config['max_new_tasks_per_instance'] and
                instance.free_space > instance.reserved_space and
                self._is_traffic_within_limit(instance)
            ]
            
            if not available_instances:
                return None
                
            # 按可配置算法排序：主要因素（小值优先），次要因素是任务计数（小值优先），第三因素是硬盘空间（大值优先）
            available_instances.sort(key=lambda x: (
                self._get_primary_sort_value(x),  # 主要因素：小值优先
                x.total_added_tasks_count,        # 次要因素：已添加任务计数小的优先
                -x.free_space                     # 第三因素：硬盘空间大的优先（使用负号）
            ))
            
            selected = available_instances[0]
            primary_sort_key = self.config.get('primary_sort_key', DEFAULT_PRIMARY_SORT_KEY)
            primary_value = self._get_primary_sort_value(selected)
            
            logger.debug(f"选择实例 {selected.name}：" 
                        f"{SUPPORTED_SORT_KEYS[primary_sort_key]}={primary_value:.1f}，"
                        f"已添加任务数={selected.total_added_tasks_count}，"
                        f"空闲空间={selected.free_space/BYTES_TO_GB:.1f}GB，"
                        f"保留空间={selected.reserved_space/BYTES_TO_GB:.1f}GB，"
                        f"流量={selected.traffic_out/BYTES_TO_GB:.2f}/{selected.traffic_limit/BYTES_TO_GB:.2f}GB")
            
            return selected
            
    def _add_torrent_to_instance(self, instance: InstanceInfo, torrent: PendingTorrent) -> bool:
        """将torrent添加到指定实例"""
        try:
            add_params = {'urls': torrent.download_url}
            
            # 设置分类
            if torrent.category:
                add_params['category'] = torrent.category
                logger.info(f"为种子设置分类：{torrent.release_name} -> {torrent.category}")
                
            # 根据配置决定是否将种子添加为暂停状态（用于调试）
            if self.config.get('debug_add_stopped', False):
                add_params['is_stopped'] = True
                logger.info(f"调试模式：种子将以暂停状态添加 - {torrent.release_name}")

            result = instance.client.torrents_add(**add_params)
            
            if result and result.startswith('Ok'):
                instance.new_tasks_count += 1
                instance.total_added_tasks_count += 1  # 增加累计任务计数
                instance.active_downloads += 1  # 乐观更新，避免下一次分配读到过期下载数
                log_msg = f"成功添加种子到实例 {instance.name}：{torrent.release_name}"
                if torrent.category:
                    log_msg += f"（分类：{torrent.category}）"
                logger.info(log_msg)
                self._request_status_refresh()
                self._notify(
                    f"[种子已添加] {torrent.release_name}\n"
                    f"实例：{instance.name}\n分类：{torrent.category or '-'}"
                )
                webhook_server = getattr(self, 'webhook_server', None)
                if webhook_server:
                    webhook_server.record_event('success', torrent.release_name, instance.name)
                return True
            else:
                logger.error(f"添加种子失败 - 实例：{instance.name}，种子：{torrent.release_name}，结果：{result}")
                if not torrent.failure_notified:
                    self._notify(f"[种子添加失败] {torrent.release_name}\n实例：{instance.name}\n结果：{result}")
                    webhook_server = getattr(self, 'webhook_server', None)
                    if webhook_server:
                        webhook_server.record_event('error', torrent.release_name, instance.name)
                    torrent.failure_notified = True
                return False
                
        except Exception as e:
            logger.error(f"添加种子到实例失败 - 实例：{instance.name}，种子：{torrent.release_name}，错误：{e}")
            if not torrent.failure_notified:
                self._notify(f"[种子添加异常] {torrent.release_name}\n实例：{instance.name}\n错误：{e}")
                webhook_server = getattr(self, 'webhook_server', None)
                if webhook_server:
                    webhook_server.record_event('error', torrent.release_name, instance.name)
                torrent.failure_notified = True
            return False
            
    def _process_torrents(self) -> None:
        """处理待分配的torrent URL"""
        with self.pending_torrents_lock:
            if not self.pending_torrents:
                return
                
            # 处理所有待处理的torrent URL
            for torrent in self.pending_torrents[:]:  # 使用切片避免修改列表时的问题
                instance = self._select_best_instance()
                if instance:
                    if self._add_torrent_to_instance(instance, torrent):
                        self.pending_torrents.remove(torrent)
                else:
                    logger.warning("没有可用的实例来分配新任务，清空待处理队列")
                    dropped = [item.release_name for item in self.pending_torrents]
                    self._notify(f"[分配失败] 没有可用实例\n已丢弃 {len(dropped)} 个待处理任务")
                    if self.webhook_server:
                        for release_name in dropped:
                            self.webhook_server.record_event('error', release_name, '没有可用实例')
                    self.pending_torrents.clear()
                    break

    def _reset_task_counters(self) -> None:
        """重置任务计数器（每轮处理完成后）"""
        with self.instances_lock:
            for instance in self.instances:
                instance.new_tasks_count = 0

    def _request_status_refresh(self) -> None:
        """请求状态更新线程尽快刷新所有实例状态。"""
        self.status_refresh_event.set()

    def _wait_for_next_status_refresh(self, timeout: float) -> None:
        """等待下一次定时刷新，或被任务添加事件提前唤醒。"""
        if self.status_refresh_event.wait(timeout):
            self.status_refresh_event.clear()
            time.sleep(STATUS_REFRESH_AFTER_ADD_DELAY)
            logger.debug("收到立即刷新请求，延迟1秒后执行状态刷新")
                
    def _log_status_summary(self) -> None:
        """记录状态摘要信息"""
        with self.instances_lock:
            total_instances = len(self.instances)
            connected_count = sum(1 for i in self.instances if i.is_connected)
            disconnected_instances = [i.name for i in self.instances if not i.is_connected]
            
            status_msg = f"实例状态: {connected_count}/{total_instances} 连接正常"
            if disconnected_instances:
                status_msg += f", 断开连接: {', '.join(disconnected_instances)}"
            # 移除待处理torrent数量，因为该信息10s更新一次时效性太差
            
            logger.debug(status_msg)
                
    def status_update_thread(self) -> None:
        """状态更新线程"""
        logger.info("状态更新线程启动")
        
        while True:
            try:
                self._update_instance_status()
                self._log_status_summary()
                self._check_and_schedule_reconnects()
                self._wait_for_next_status_refresh(STATUS_REFRESH_INTERVAL)
                
            except Exception as e:
                logger.error(f"状态更新线程错误：{e}")
                time.sleep(ERROR_RETRY_SLEEP)
                
    def task_processor_thread(self) -> None:
        """任务处理线程"""
        logger.info("任务处理线程启动")
        
        while True:
            try:
                # 记录当前待处理的种子数量（更及时的信息）
                with self.pending_torrents_lock:
                    pending_count = len(self.pending_torrents)
                
                if pending_count > 0:
                    logger.debug(f"处理 {pending_count} 个待分配的种子")
                
                self._process_torrents()
                self._reset_task_counters()
                time.sleep(TASK_PROCESSOR_SLEEP)
                
            except Exception as e:
                logger.error(f"任务处理线程错误：{e}")
                time.sleep(ERROR_RETRY_SLEEP)
                
    def run(self) -> None:
        """运行负载均衡器"""
        logger.info("qBittorrent负载均衡器启动")
        
        # 启动状态更新线程
        status_thread = threading.Thread(target=self.status_update_thread, daemon=True)
        status_thread.start()
        
        # 启动任务处理线程
        task_thread = threading.Thread(target=self.task_processor_thread, daemon=True)
        task_thread.start()
        
        try:
            # 主线程保持运行
            while True:
                time.sleep(DEFAULT_SLEEP_TIME)
        except KeyboardInterrupt:
            logger.info("收到停止信号，正在关闭...")
            if self.webhook_server:
                self.webhook_server.stop()
                logger.info("Webhook服务器已停止")
            self.telegram_notifier.stop()


def main() -> int:
    """主函数"""
    try:
        balancer = QBittorrentLoadBalancer()
        balancer.run()
        return 0
    except Exception as e:
        logger.error(f"程序启动失败：{e}")
        return 1


if __name__ == "__main__":
    exit(main()) 
