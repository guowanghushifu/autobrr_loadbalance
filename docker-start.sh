#!/bin/bash

# qBittorrent 负载均衡器 Docker 启动脚本

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_message() {
    echo -e "${2}${1}${NC}"
}

# 检查Docker是否安装
check_docker() {
    if ! command -v docker &> /dev/null; then
        print_message "Docker 未安装！请先安装 Docker。" $RED
        exit 1
    fi
    
    if ! docker compose version &> /dev/null; then
        print_message "Docker Compose Plugin 未安装或不可用！" $RED
        exit 1
    fi

    if ! docker info &> /dev/null; then
        print_message "无法连接 Docker daemon，请检查服务状态和当前用户权限。" $RED
        exit 1
    fi
}

# 创建必要的目录
create_directories() {
    print_message "创建必要的目录..." $BLUE
    mkdir -p logs
}

# 检查配置文件
check_config() {
    if [ ! -f "config.json" ]; then
        if [ ! -f "config.json.example" ]; then
            print_message "未找到配置模板文件 config.json.example！" $RED
            exit 1
        fi
        
        print_message "未找到配置文件 config.json，从模板复制..." $YELLOW
        cp config.json.example config.json
        print_message "已创建配置文件，请修改 config.json 中的 qBittorrent 实例信息！" $YELLOW
        print_message "配置完成后再次运行此脚本。" $YELLOW
        exit 0
    fi
}

# 构建Docker镜像
build_image() {
    print_message "构建 Docker 镜像..." $BLUE
    docker build --pull -t qbittorrent-loadbalancer .
}

# 让非root容器用户可以写入Dashboard配置和日志目录
prepare_permissions() {
    print_message "设置配置文件和日志目录权限..." $BLUE
    docker run --rm \
        --user root \
        --entrypoint sh \
        -v "$(pwd)/config.json:/app/config.json" \
        -v "$(pwd)/logs:/app/logs" \
        qbittorrent-loadbalancer \
        -c 'chown appuser:appuser /app/config.json && chown -R appuser:appuser /app/logs'
}

get_server_ip() {
    local address
    for address in $(hostname -I 2>/dev/null); do
        case "$address" in
            127.*|*:* ) continue ;;
            * ) echo "$address"; return ;;
        esac
    done
    address=$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
    if [ -n "$address" ]; then
        echo "$address"
        return
    fi
    hostname
}

# 显示使用帮助
show_help() {
    echo "用法: $0 [选项]"
    echo ""
    echo "选项:"
    echo "  start           启动负载均衡器服务"
    echo "  stop            停止服务"
    echo "  restart         重启服务"
    echo "  logs            查看日志"
    echo "  build           构建镜像"
    echo "  prod            启动生产环境（同start）"
    echo "  status          查看服务状态"
    echo "  clean           清理当前Compose项目的容器"
    echo "  help            显示此帮助信息"
}

# 主要功能
case "${1:-start}" in
    "start"|"prod")
        check_docker
        create_directories
        check_config
        build_image
        prepare_permissions
        print_message "启动负载均衡器..." $GREEN
        docker compose up -d
        server_ip=$(get_server_ip)
        print_message "服务已启动！" $GREEN
        print_message "服务地址: http://${server_ip}:50000" $BLUE
        print_message "Dashboard: http://${server_ip}:50000/dashboard" $BLUE
        print_message "健康检查: curl http://${server_ip}:50000/health" $BLUE
        print_message "查看日志: ./docker-start.sh logs" $BLUE
        ;;
    
    "stop")
        print_message "停止服务..." $YELLOW
        docker compose down 2>/dev/null || true
        print_message "服务已停止！" $GREEN
        ;;
    
    "restart")
        $0 stop
        sleep 2
        $0 start
        ;;
    
    "logs")
        docker compose logs -f qbittorrent-loadbalancer 2>/dev/null || \
        print_message "未找到运行中的服务" $RED
        ;;
    
    "build")
        check_docker
        build_image
        print_message "镜像构建完成！" $GREEN
        ;;
    
    "status")
        check_docker
        print_message "服务状态:" $BLUE
        docker compose ps
        ;;
    
    "clean")
        check_docker
        print_message "清理当前Compose项目的容器..." $YELLOW
        docker compose down --remove-orphans
        print_message "当前项目容器已清理！" $GREEN
        ;;
    
    "help"|"-h"|"--help")
        show_help
        ;;
    
    *)
        print_message "未知选项: $1" $RED
        show_help
        exit 1
        ;;
esac
