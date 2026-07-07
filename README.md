# qBittorrent 智能负载均衡器

为 qBittorrent 多实例设计的智能负载均衡器，通过 webhook 接收 [autobrr](https://github.com/autobrr/autobrr) 的种子添加请求，自动分配到最优实例。

## 核心功能

- **智能负载均衡**: 根据上传/下载速度、活跃下载数智能选择最佳实例
- **Webhook 集成**: 与 autobrr 无缝集成，实时处理种子添加
- **自动重连**: 实例断开时自动重连
- **自动 Announce**: 智能重新汇报种子给 Tracker
- **流量监控**: 支持流量限制检查（可选）

## 快速开始

### Docker 部署（推荐）

1. **下载项目**
```bash
git clone <your-repo-url>
cd <repo-folder>
```

2. **启动服务**
```bash
chmod +x docker-start.sh
./docker-start.sh
```
首次运行会自动创建配置模板。

3. **修改配置**
编辑生成的 `config.json`，配置你的 qBittorrent 实例信息。

4. **启动服务**
```bash
./docker-start.sh start
```

5. **配置 autobrr**
在 autobrr 中添加 Webhook Action：
- URL: `http://<your-server-ip>:5000<your-webhook-path>`
- Body:
```json
{
  "release_name": "{{.TorrentName}}",
  "indexer": "{{.Indexer}}",
  "download_url": "{{.TorrentUrl}}"
}
```
图示：
![PixPin_2025-07-28_08-41-20.png](https://image.dooo.ng/c/2025/07/28/6886c78fc7448.webp)

### 本地运行

```bash
pip install -r requirements.txt
cp config.json.example config.json
# 编辑 config.json
python run.py
```

## 配置说明

### 必需配置

| 参数 | 说明 | 示例 |
|------|------|------|
| `qbittorrent_instances` | qBittorrent 实例列表 | 见配置示例 |
| `webhook_path` | Webhook 访问路径（**必须随机化**） | `/webhook/secure-random-string` |

### 常用配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `webhook_port` | `5000` | Webhook 监听端口 |
| `primary_sort_key` | `upload_speed` | 负载均衡策略：`upload_speed`/`download_speed`/`upload_download_speed`/`active_downloads`/`total_downloads` |
| `max_new_tasks_per_instance` | `2` | 单实例单轮最大新任务数 |
| `max_announce_retries` | `30` | 种子最大汇报重试次数 |
| `fast_announce_interval` | `3` | 快速检查间隔（2-10秒），正常检查为2倍该值 |
| `connection_timeout` | `6` | 连接超时时间（秒）|
| `debug_add_stopped` | `false` | 调试模式：新种子暂停添加 |
| `fast_announce_enabled` | `false` | 快速汇报模式开关，谨慎使用 |
| `fast_announce_category_blacklist` | `[]` | 快速汇报分类黑名单，指定分类的种子不触发快速汇报 |

`total_downloads` 的计算方式为：活跃下载数 + 0.5 × 等待下载数。等待下载数指 qBittorrent 中已添加但还未开始传输数据的下载任务，例如 `stalledDL`、`queuedDL`、`metaDL` 状态。

`upload_download_speed` 的计算方式为：上传速率 × 0.6 + 下载速率 × 0.4。默认系数可在 `main.py` 顶部修改：`UPLOAD_DOWNLOAD_SORT_UPLOAD_WEIGHT` 和 `UPLOAD_DOWNLOAD_SORT_DOWNLOAD_WEIGHT`。

⚠️ 警告：fast_announce_enabled打开可能会导致你的账号被站点警告、封禁等后果，如果你打开，责任自负

### 可选配置（流量监控）

| 参数 | 说明 |
|------|------|
| `traffic_check_url` | 流量检查 API URL |
| `traffic_limit` | 流量限制（MB），超限实例会被跳过 |
| `reserved_space` | 需要保留的空闲空间（MB），低于此值的实例会被跳过 |

流量限制参数traffic_check_url和traffic_limit支持两种应用场景：
1. 配套`https://github.com/guowanghushifu/netcup-traffic-tester`使用，traffic_limit填2000000
2. 配套`https://github.com/guowanghushifu/vnstat-traffic-exporter`使用，按需配置流量

流量 API 需返回格式, in和out表示入站和出站流量，单位为MB：`{"in":1421.72,"out":11777.19,"start_date":"2025-07-19"}`

## 配置示例
请参考config.json.example

## 安全说明

⚠️ **重要**: `webhook_path` 必须设置为长且随机的字符串，这是应用安全的核心。

- ❌ 错误: `/webhook`, `/autobrr`
- ✅ 正确: `/webhook/secure-a8f9c2e1-4b3d-9876-abcd-ef0123456789`

## Docker 管理命令

```bash
./docker-start.sh start     # 启动服务
./docker-start.sh stop      # 停止服务
./docker-start.sh restart   # 重启服务
./docker-start.sh logs      # 查看日志
./docker-start.sh status    # 查看状态
```

## API 接口

- `GET /health`: 健康检查
- `POST <webhook_path>`: 接收 autobrr 种子添加请求

## 日志

- 日志目录: `./logs/`
- 主日志: `qbittorrent_loadbalancer.log`
- 错误日志: `qbittorrent_error.log`

## 故障排除

1. **连接失败**: 检查 qBittorrent Web UI 设置和网络连通性
2. **Webhook 无响应**: 确认 `webhook_path` 配置正确
3. **调试模式**: 设置 `debug_add_stopped: true` 暂停新种子便于调试 
