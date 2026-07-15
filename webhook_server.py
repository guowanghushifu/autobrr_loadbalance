#!/usr/bin/env python3
"""
Webhook 服务器模块
接收 autobrr 的 webhook 通知并处理种子数据
"""

import logging
import hmac
import json
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, render_template

logger = logging.getLogger(__name__)


class WebhookServer:
    """Webhook服务器，接收autobrr通知"""
    
    def __init__(self, torrent_manager: 'QBittorrentLoadBalancer', config: Dict[str, Any]):
        self.torrent_manager = torrent_manager
        self.config = config
        self.app = Flask(__name__)
        self.app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
        self.app.logger.disabled = True  # 禁用Flask的默认日志
        self.server_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.events = deque(maxlen=int(config.get('dashboard', {}).get('event_limit', 100)))
        self.events_lock = threading.Lock()
        
        # webhook配置
        self.webhook_port = config.get('webhook_port', 5000)
        self.webhook_path = config.get('webhook_path', '/webhook')
        dashboard = config.get('dashboard', {})
        self.dashboard_enabled = bool(dashboard.get('enabled', False))
        self.dashboard_username = str(dashboard.get('username', '')).strip()
        self.dashboard_password = str(dashboard.get('password', ''))
        if self.dashboard_enabled and (not self.dashboard_username or not self.dashboard_password):
            logger.warning("Dashboard已启用但未配置用户名或密码，Dashboard将被禁用")
            self.dashboard_enabled = False
        # 安全提示：建议设置复杂的webhook_path作为安全措施
        
        self._setup_routes()
    
    def _setup_routes(self):
        """设置路由"""
        
        @self.app.route('/health', methods=['GET'])
        def health_check():
            """健康检查接口"""
            return jsonify({
                'status': 'ok',
                'timestamp': datetime.now().isoformat(),
                'instances_connected': len([i for i in self.torrent_manager.instances if i.is_connected])
            })
        
        @self.app.route(self.webhook_path, methods=['POST'])
        def webhook_handler():
            """处理webhook请求"""
            source_ip = request.remote_addr or ''
            if not self.torrent_manager.is_webhook_ip_allowed(source_ip):
                logger.warning("拒绝非白名单Webhook请求：%s", source_ip or 'Unknown')
                self.record_event('blocked', 'Webhook request', source_ip or 'Unknown', source_ip)
                self.torrent_manager._notify(f"[Webhook已拒绝] 来源IP：{source_ip or 'Unknown'}")
                return jsonify({'error': 'Source IP is not allowed'}), 403
            try:
                # 获取请求数据
                data = request.get_json()
                if not data:
                    logger.error("webhook请求缺少JSON数据")
                    self.record_event('error', 'Invalid webhook payload', '缺少JSON数据', source_ip)
                    return jsonify({'error': 'No JSON data'}), 400
                
                logger.info(f"收到webhook通知: {data.get('release_name', 'Unknown')}")
                
                # 处理种子数据
                success = self._process_webhook_data(data, source_ip)
                
                if success:
                    return jsonify({'status': 'success', 'message': 'Torrent queued'})
                else:
                    return jsonify({'status': 'error', 'message': 'Failed to process torrent'}), 500
                    
            except Exception as e:
                logger.error(f"处理webhook请求时出错: {e}")
                self.record_event('error', data.get('release_name', 'Unknown') if 'data' in locals() else 'Unknown', str(e), source_ip)
                return jsonify({'error': 'Internal server error'}), 500

        if self.dashboard_enabled:
            self._setup_dashboard_routes()

    def _dashboard_auth_required(self, handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            authorization = request.authorization
            valid = bool(
                authorization
                and hmac.compare_digest(authorization.username or '', self.dashboard_username)
                and hmac.compare_digest(authorization.password or '', self.dashboard_password)
            )
            if not valid:
                return (
                    jsonify({'error': 'Authentication required'}),
                    401,
                    {'WWW-Authenticate': 'Basic realm="Load Balancer Dashboard"'},
                )
            return handler(*args, **kwargs)
        return wrapped

    def _setup_dashboard_routes(self):
        @self.app.route('/dashboard', methods=['GET'])
        @self._dashboard_auth_required
        def dashboard():
            return render_template('dashboard.html', webhook_path=self.webhook_path)

        @self.app.route('/api/dashboard/status', methods=['GET'])
        @self._dashboard_auth_required
        def dashboard_status():
            snapshot = self.torrent_manager.get_dashboard_snapshot()
            with self.events_lock:
                snapshot['events'] = list(self.events)
            return jsonify(snapshot)

        @self.app.route('/api/dashboard/whitelist', methods=['POST'])
        @self._dashboard_auth_required
        def add_whitelist_entry():
            payload = request.get_json(silent=True) or {}
            entry = str(payload.get('entry', '')).strip()
            if not entry:
                return jsonify({'error': 'IP or CIDR is required'}), 400
            try:
                normalized = self.torrent_manager.add_webhook_whitelist_entry(entry)
            except ValueError:
                return jsonify({'error': 'Invalid IP or CIDR'}), 400
            except OSError as exc:
                logger.error("保存Webhook白名单失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            self.record_event('config', 'Webhook whitelist', f'添加 {normalized}', request.remote_addr or '')
            return jsonify({'entry': normalized}), 201

        @self.app.route('/api/dashboard/whitelist', methods=['DELETE'])
        @self._dashboard_auth_required
        def delete_whitelist_entry():
            payload = request.get_json(silent=True) or {}
            entry = str(payload.get('entry', '')).strip()
            try:
                removed = self.torrent_manager.remove_webhook_whitelist_entry(entry)
            except OSError as exc:
                logger.error("保存Webhook白名单失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            if not removed:
                return jsonify({'error': 'Whitelist entry not found'}), 404
            self.record_event('config', 'Webhook whitelist', f'移除 {entry}', request.remote_addr or '')
            return jsonify({'status': 'deleted'})

        @self.app.route('/api/dashboard/instances', methods=['POST'])
        @self._dashboard_auth_required
        def save_instance():
            payload = request.get_json(silent=True) or {}
            original_name = str(payload.pop('original_name', '')).strip()
            try:
                result = self.torrent_manager.upsert_qbittorrent_instance(payload, original_name)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            except OSError as exc:
                logger.error("保存qBittorrent实例失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            action = '更新' if original_name else '添加'
            self.record_event('config', 'qBittorrent instance', f"{action} {result['name']}", request.remote_addr or '')
            return jsonify(result), 200 if original_name else 201

        @self.app.route('/api/dashboard/instances', methods=['DELETE'])
        @self._dashboard_auth_required
        def delete_instance():
            payload = request.get_json(silent=True) or {}
            name = str(payload.get('name', '')).strip()
            try:
                removed = self.torrent_manager.delete_qbittorrent_instance(name)
            except OSError as exc:
                logger.error("删除qBittorrent实例失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            if not removed:
                return jsonify({'error': 'Instance not found'}), 404
            self.record_event('config', 'qBittorrent instance', f'删除 {name}', request.remote_addr or '')
            return jsonify({'status': 'deleted'})

        @self.app.route('/api/dashboard/instances/clone', methods=['POST'])
        @self._dashboard_auth_required
        def clone_instance():
            payload = request.get_json(silent=True) or {}
            try:
                result = self.torrent_manager.clone_qbittorrent_instance(str(payload.get('name', '')).strip())
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 404
            except OSError as exc:
                logger.error("克隆qBittorrent实例失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            self.record_event('config', 'qBittorrent instance', f"克隆 {result['name']}", request.remote_addr or '')
            return jsonify(result), 201

        @self.app.route('/api/dashboard/config/import', methods=['POST'])
        @self._dashboard_auth_required
        def import_config():
            uploaded = request.files.get('config')
            if uploaded is None:
                return jsonify({'error': 'config.json file is required'}), 400
            try:
                imported = json.load(uploaded.stream)
                result = self.torrent_manager.import_config(imported)
            except (ValueError, json.JSONDecodeError) as exc:
                return jsonify({'error': str(exc)}), 400
            except OSError as exc:
                logger.error("导入config.json失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            self.record_event('config', 'config.json', f"导入 {result['instances']} 个实例", request.remote_addr or '')
            return jsonify(result)

        @self.app.route('/api/dashboard/telegram', methods=['POST'])
        @self._dashboard_auth_required
        def save_telegram_config():
            payload = request.get_json(silent=True) or {}
            try:
                result = self.torrent_manager.update_telegram_config(payload)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            except OSError as exc:
                logger.error("保存Telegram配置失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            state = '启用' if result['enabled'] else '停用'
            self.record_event('config', 'Telegram Bot', state, request.remote_addr or '')
            return jsonify(result)

        @self.app.route('/api/dashboard/timezone', methods=['POST'])
        @self._dashboard_auth_required
        def save_dashboard_timezone():
            payload = request.get_json(silent=True) or {}
            try:
                result = self.torrent_manager.update_dashboard_timezone(payload.get('timezone', ''))
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            except OSError as exc:
                logger.error("保存Dashboard时区失败：%s", exc)
                return jsonify({'error': 'Failed to persist configuration'}), 500
            self.record_event('config', 'Dashboard timezone', result['name'], request.remote_addr or '')
            return jsonify(result)

        @self.app.route('/api/dashboard/telegram/test', methods=['POST'])
        @self._dashboard_auth_required
        def test_telegram_config():
            try:
                self.torrent_manager.send_telegram_test()
            except Exception as exc:
                logger.error("Telegram测试通知失败：%s", exc)
                return jsonify({'error': str(exc)}), 502
            return jsonify({'status': 'sent'})

        @self.app.route('/api/dashboard/logs', methods=['GET'])
        @self._dashboard_auth_required
        def dashboard_logs():
            try:
                after = max(0, int(request.args.get('after', 0)))
                limit = max(1, min(int(request.args.get('limit', 500)), 1000))
            except ValueError:
                return jsonify({'error': 'Invalid log cursor or limit'}), 400
            return jsonify(self.torrent_manager.get_dashboard_logs(after, limit))

    def record_event(self, status: str, release_name: str, detail: str = '', source_ip: str = '') -> None:
        event = {
            'status': status,
            'release_name': release_name,
            'detail': detail,
            'source_ip': source_ip,
            'timestamp': datetime.now().astimezone().isoformat(),
        }
        with self.events_lock:
            self.events.appendleft(event)

    def _process_webhook_data(self, data: Dict[str, Any], source_ip: str = '') -> bool:
        """处理webhook数据"""
        try:
            torrent_data = self._extract_torrent_data(data)
            if not torrent_data:
                return False
            
            release_name, download_url, indexer, category = torrent_data
            logger.info(f"接收到种子：{release_name} (来源：{indexer})")
            
            # 传递给负载均衡器处理
            self.torrent_manager.add_pending_torrent(
                download_url=download_url,
                release_name=release_name,
                category=category or indexer
            )
            self.record_event('queued', release_name, category or indexer, source_ip)
            return True
            
        except Exception as e:
            logger.error(f"处理webhook数据时出错: {e}")
            self.record_event('error', data.get('release_name', 'Unknown'), str(e), source_ip)
            return False
    
    def _extract_torrent_data(self, data: Dict[str, Any]) -> Optional[tuple]:
        """从webhook数据中提取种子信息"""
        release_name = data.get('release_name', '')
        download_url = data.get('download_url', '')
        indexer = data.get('indexer', '')
        category = data.get('category', '')
        
        if not release_name:
            logger.error("webhook数据缺少种子名称")
            return None
        
        if not download_url:
            logger.error("webhook数据缺少下载链接")
            return None
        
        return release_name, download_url, indexer, category
    

    
    def start(self):
        """启动webhook服务器"""
        if self.is_running:
            return
        
        self.is_running = True
        logger.info(f"启动webhook服务器: http://0.0.0.0:{self.webhook_port}{self.webhook_path}")
        
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        
        # 等待确保服务器启动
        time.sleep(1)
        
        status_msg = "webhook服务器启动成功" if self.is_running else "webhook服务器启动失败"
        logger.info(status_msg) if self.is_running else logger.error(status_msg)
    
    def _run_server(self):
        """运行Flask服务器"""
        try:
            self.app.run(
                host='0.0.0.0',
                port=self.webhook_port,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        except Exception as e:
            logger.error(f"webhook服务器启动失败: {e}")
            self.is_running = False
    
    def stop(self):
        """停止webhook服务器"""
        if not self.is_running:
            return
        
        self.is_running = False
        logger.info("webhook服务器已停止")
    
