#!/usr/bin/env python3
"""
qBittorrent Load Balancer
监控torrent文件并智能分配到多个qBittorrent实例
"""

import json
import os
import time
import threading
import logging
import csv
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import qbittorrentapi
from webhook_server import WebhookServer


# 配置常量
DEFAULT_CONFIG_FILE = "config.json"

# 时间间隔常量（秒）
DEFAULT_SLEEP_TIME = 1
TASK_PROCESSOR_SLEEP = 1
ERROR_RETRY_SLEEP = 5
RECONNECT_INTERVAL = 180
CONNECTION_TIMEOUT = 10
STATUS_REFRESH_AFTER_ADD_DELAY = 1

# 网络和存储常量
BYTES_TO_KB = 1024
KIB_PER_MIB = 1024
BYTES_TO_GB = 1024 ** 3
BYTES_TO_TB = 1024 ** 4
MAX_RECONNECT_ATTEMPTS = 1

# 种子汇报相关常量
ANNOUNCE_WINDOW_TOLERANCE = 5
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
    reserved_space: int = 0  # 需要保留的空闲空间 (bytes)
    last_update: datetime = field(default_factory=datetime.now)
    is_reconnecting: bool = False  # 是否正在重连中


@dataclass
class PendingTorrent:
    """待处理的torrent"""
    download_url: str
    release_name: str
    category: Optional[str] = None


class QBittorrentLoadBalancer:
    """qBittorrent负载均衡器"""
    
    def __init__(self, config_file: str = DEFAULT_CONFIG_FILE):
        self.config = self._load_config(config_file)        
        self.instances: List[InstanceInfo] = []
        self.pending_torrents: List[PendingTorrent] = []
        self.pending_torrents_lock = threading.Lock()
        self.instances_lock = threading.Lock()
        self.status_refresh_event = threading.Event()
        self.announce_retry_counts = {} # 用于跟踪每个种子的汇报重试次数
        
        # 重新配置日志（支持文件输出）
        self._setup_logging()
        
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
        
        logger = setup_logging(log_dir)
        
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
            
        # 验证快速汇报分类黑名单配置
        blacklist = self.config.get('fast_announce_category_blacklist')
        if blacklist is not None:
            if not isinstance(blacklist, list):
                logger.warning(f"fast_announce_category_blacklist 配置格式错误，必须是数组，当前类型：{type(blacklist)}，已重置为空数组")
                self.config['fast_announce_category_blacklist'] = []
            else:
                # 验证数组中的每个元素都是字符串
                valid_blacklist = []
                for item in blacklist:
                    if isinstance(item, str):
                        valid_blacklist.append(item)
                    else:
                        logger.warning(f"黑名单中包含非字符串项目：{item} (类型：{type(item)})，已忽略")
                
                self.config['fast_announce_category_blacklist'] = valid_blacklist
                if valid_blacklist:
                    logger.info(f"快速汇报分类黑名单已配置，包含 {len(valid_blacklist)} 个分类：{valid_blacklist}")
                else:
                    logger.info("快速汇报分类黑名单为空，所有分类都将执行快速汇报")
        else:
            # 如果没有配置黑名单，设置为空数组
            self.config['fast_announce_category_blacklist'] = []
            logger.info("未配置快速汇报分类黑名单，所有分类都将执行快速汇报")
            
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
        # 设置快速汇报间隔默认值，并限制在2-10秒范围内
        fast_interval = self.config.get('fast_announce_interval', 3)
        if not isinstance(fast_interval, (int, float)) or fast_interval < 2 or fast_interval > 10:
            logger.warning(f"fast_announce_interval 值无效 ({fast_interval})，必须在2-10秒范围内，使用默认值4秒")
            fast_interval = 3
        self.config['fast_announce_interval'] = fast_interval
        
        logger.info(f"状态更新间隔配置：快速检查={fast_interval}秒，正常检查={fast_interval * 2}秒")

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
            reserved_space_mb = config.get('reserved_space', 21 * 1024)  # 默认21GB
            reserved_space_bytes = int(float(reserved_space_mb) * 1024 * 1024)  # MB转字节
        except (ValueError, TypeError) as e:
            logger.warning(f"实例 {config.get('name', 'Unknown')} 保留空间值转换失败：{e}，设置为默认值21GB")
            reserved_space_bytes = 21 * BYTES_TO_GB
            
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
                return True
                
            except Exception as e:
                logger.warning(f"重连尝试 {attempt + 1}/{max_attempts} 失败：{instance.name}，错误：{e}")
                if attempt < max_attempts - 1:
                    time.sleep(2)  # 每次重连尝试间等待2秒
                    
        logger.error(f"重连彻底失败：{instance.name}")
        
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
            

                
    def _update_instance_status(self) -> None:
        """更新所有实例的状态信息"""
        with self.instances_lock:
            for instance in self.instances:
                if instance.is_connected:
                    self._update_single_instance(instance)
                    
    def _update_single_instance(self, instance: InstanceInfo) -> None:
        """更新单个实例的状态信息"""
        def _try_update_instance():
            """尝试更新实例状态的内部函数"""
            maindata = instance.client.sync_maindata()
            self._update_instance_metrics(instance, maindata)
            self._process_instance_announces(instance, maindata)
            #self._add_peers_for_retry_torrents(instance, maindata)
            #self._save_torrent_peers_to_csv(instance, maindata)
        
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
                    
    def _update_instance_metrics(self, instance: InstanceInfo, maindata: dict) -> None:
        """使用sync/maindata的结果更新单个实例的状态信息"""
        server_state = maindata.get('server_state', {})
        
        # 从server_state获取全局统计信息和硬盘空间
        instance.upload_speed = server_state.get('up_info_speed', 0) / BYTES_TO_KB
        instance.download_speed = server_state.get('dl_info_speed', 0) / BYTES_TO_KB
        instance.free_space = server_state.get('free_space_on_disk', 0)
        
        # 从torrents信息计算活跃下载数
        all_torrents = maindata.get('torrents', {}).values()
        instance.active_downloads = len([t for t in all_torrents if t.state == 'downloading'])
        instance.waiting_downloads_count = len([
            t for t in all_torrents if t.state in WAITING_DOWNLOAD_STATES
        ])
        
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
                   
    def _process_instance_announces(self, instance: InstanceInfo, maindata: dict) -> None:
        """处理实例的种子汇报检查"""
        # 如果debug_add_stopped为True，直接返回，不做任何处理
        if self.config.get('debug_add_stopped', False):
            return
            
        # 如果快速汇报开关未启用，直接返回，不做任何处理
        if not self.config.get('fast_announce_enabled', False):
            return

        max_retries = self.config.get('max_announce_retries', 12)
        error_keywords = ["unregistered", "not registered", "not found", "not exist"]
        current_time = datetime.now()

        all_torrents_items = maindata.get('torrents', {}).items()

        for torrent_hash, torrent in all_torrents_items:
            age_seconds = (current_time - datetime.fromtimestamp(torrent.added_on)).total_seconds()
            is_completed = torrent.progress == 1.0

            # 条件1：如果种子已完成或添加超过2分钟，则确保其已从监控列表中移除，并跳过
            if (is_completed and age_seconds > 60) or age_seconds > 140 or age_seconds < 2:
                if torrent_hash in self.announce_retry_counts:
                    del self.announce_retry_counts[torrent_hash]
                    if is_completed:
                        reason = "已完成"
                    elif age_seconds > 120:
                        reason = "超过2分钟"
                    else:
                        reason = "添加时间小于2秒"
                    logger.debug(f"停止汇报监控: {torrent.name} (原因: {reason})")
                continue
                
            # 检查种子分类是否在快速汇报黑名单中
            blacklist = self.config.get('fast_announce_category_blacklist', [])
            if blacklist and hasattr(torrent, 'category') and torrent.category in blacklist:
                # 如果种子分类在黑名单中，从监控列表中移除并跳过
                if torrent_hash in self.announce_retry_counts:
                    del self.announce_retry_counts[torrent_hash]
                    logger.debug(f"跳过快速汇报: {torrent.name} (分类 '{torrent.category}' 在黑名单中)")
                continue
                
            # 条件2：如果种子未完成且未超过2分钟，则进行汇报检查
            # 初始化或递增重试计数器
            if torrent_hash not in self.announce_retry_counts:
                self.announce_retry_counts[torrent_hash] = 0
            
            # 每次进入函数时递增计数器
            self.announce_retry_counts[torrent_hash] += 1
            current_retries = self.announce_retry_counts[torrent_hash]
            
            logger.debug(f"汇报检查: {torrent.name} (第{current_retries}次检查，最大{max_retries}次)")

            # 检查是否达到1分钟或者2分钟且种子仍未完成，如果是则强制汇报
            fast_interval = self.config.get('fast_announce_interval', 3)
            first_force_announce = int(60 / fast_interval)
            second_force_announce = int(120 / fast_interval)
            if (current_retries == first_force_announce or current_retries == second_force_announce) and not is_completed:
                logger.info(f"达到特定次数({current_retries})且种子未完成，强制汇报: {torrent.name}")
                self._announce_torrent(instance, torrent, torrent_hash, f"强制汇报(第{current_retries}次检查)")
                continue

            # 如果还没到最大重试次数，继续正常的汇报条件检查
            if current_retries < max_retries:
                # 检查汇报条件
                needs_announce = False
                reason = []

                try:
                    # 1. 检查Tracker状态
                    trackers = instance.client.torrents_trackers(torrent_hash=torrent_hash)
                    
                    # Filter out non-HTTP trackers and special trackers like DHT, PEX, LSD
                    filtered_trackers = []
                    for t in trackers:
                        # DHT, PEX, and LSD are peer sources, not trackers. The API returns them
                        # in the tracker list, but their 'url' is just a name like 'dht'.
                        if t.url.lower() in ('dht', 'pex', 'lsd'):
                            continue
                        if not t.url.startswith(('http://', 'https://')):
                            continue
                        filtered_trackers.append(t)

                    if not filtered_trackers:
                        logger.info(f"[{instance.name}] Announce check for '{torrent.name}': No valid HTTP trackers found, skipping.")
                        continue

                    all_trackers_failed = all(t.status in [1, 3, 4] for t in filtered_trackers)
                    has_error_keyword = any(keyword in t.msg.lower() for t in filtered_trackers for keyword in error_keywords)

                    if all_trackers_failed:
                        needs_announce = True
                        reason.append("所有tracker状态异常")
                    if has_error_keyword:
                        needs_announce = True
                        reason.append("发现tracker错误信息")

                    # 2. 检查Peer数量
                    if torrent.progress < 0.8 and torrent.num_leechs < 2:
                        needs_announce = True
                        reason.append(f"Peer数量不足({torrent.num_leechs})")

                    # 执行汇报
                    if needs_announce:
                        self._announce_torrent(instance, torrent, torrent_hash, ", ".join(reason))

                except Exception as e:
                    logger.warning(f"处理 {torrent.name} 的汇报时出错: {e}")

    def _announce_torrent(self, instance: InstanceInfo, torrent: any, torrent_hash: str, reason: str) -> None:
        """对单个种子执行announce"""
        try:
            instance.client.torrents_reannounce(torrent_hashes=torrent_hash)
            current_retries = self.announce_retry_counts.get(torrent_hash, 0)
            logger.info(f"触发汇报: {torrent.name} (原因: {reason}) | "
                        f"尝试次数: {current_retries}")
        except Exception as e:
            logger.warning(f"汇报失败: {torrent.name}，错误: {e}")
    
    def _save_torrent_peers_to_csv(self, instance: InstanceInfo, maindata: dict) -> None:
        """保存种子peer列表到CSV文件"""
        all_torrents_items = maindata.get('torrents', {}).items()
        
        for torrent_hash, torrent in all_torrents_items:
            # 检查种子是否在announce_retry_counts中
            if torrent_hash not in self.announce_retry_counts:
                continue
                
            # 检查种子状态是否为正在下载中
            if torrent.state != 'downloading':
                continue
            
            # 检查./logs目录中是否已有以此hash命名的csv文件
            csv_filename = f"./logs/{torrent_hash}.csv"
            if os.path.exists(csv_filename):
                continue
                
            try:
                # 获取种子的peer列表
                # 使用qbittorrent-api库提供的官方方法
                peers_data = instance.client.sync_torrent_peers(torrent_hash=torrent_hash)
                peers = peers_data.get('peers', {})
                
                if not peers:
                    logger.debug(f"种子 {torrent.name} 没有peer连接")
                    continue
                
                # 确保logs目录存在
                os.makedirs('./logs', exist_ok=True)
                
                # 保存peer信息到CSV文件
                with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['ip', 'port', 'client', 'country', 'downloaded', 'uploaded', 'progress']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    
                    # 写入表头
                    writer.writeheader()
                    
                    # 写入peer数据
                    for peer_id, peer_info in peers.items():
                        writer.writerow({
                            'ip': peer_info.get('ip', ''),
                            'port': peer_info.get('port', ''),
                            'client': peer_info.get('client', ''),
                            'country': peer_info.get('country', ''),
                            'downloaded': peer_info.get('downloaded', 0),
                            'uploaded': peer_info.get('uploaded', 0),
                            'progress': peer_info.get('progress', 0)
                        })
                
                logger.info(f"已保存种子 {torrent.name} 的peer列表到 {csv_filename}，共 {len(peers)} 个peer")
                
            except Exception as e:
                logger.warning(f"保存种子 {torrent.name} 的peer列表失败: {e}")
                    

    def _add_peers_for_retry_torrents(self, instance: InstanceInfo, maindata: dict) -> None:
        """为重试次数为1的种子添加指定的peer"""
        try:
            # 预定义的peer列表
            peers_to_add = ["213.227.151.211:30957", "45.87.251.103:58929"]
            
            # 获取当前实例的种子列表
            torrents_in_instance = maindata.get('torrents', {})
            
            # 只处理当前实例中存在且重试次数为1的种子
            for torrent_hash, retry_count in self.announce_retry_counts.items():
                # 检查种子是否在当前实例中存在且重试次数为1
                if retry_count == 1 and torrent_hash in torrents_in_instance:
                    try:
                        # 为种子添加peer
                        instance.client.torrents_add_peers(torrent_hashes=torrent_hash, peers=peers_to_add)
                        logger.info(f"已为种子 {torrent_hash} 添加peer: {', '.join(peers_to_add)} (实例: {instance.name})")
                    except Exception as e:
                        logger.warning(f"为种子 {torrent_hash} 添加peer失败：{e} (实例: {instance.name})")
                        
        except Exception as e:
            logger.error(f"添加peer过程中发生错误：{instance.name}，错误：{e}")


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
                return True
            else:
                logger.error(f"添加种子失败 - 实例：{instance.name}，种子：{torrent.release_name}，结果：{result}")
                return False
                
        except Exception as e:
            logger.error(f"添加种子到实例失败 - 实例：{instance.name}，种子：{torrent.release_name}，错误：{e}")
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
                              
                # 根据是否有待重试的汇报任务来调整检查频率
                fast_interval = self.config['fast_announce_interval']
                if self.announce_retry_counts:
                    self._wait_for_next_status_refresh(fast_interval)  # 有待重试任务时的快速检查频率
                else:
                    self._wait_for_next_status_refresh(fast_interval * 2)  # 正常情况下的检查频率
                
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
