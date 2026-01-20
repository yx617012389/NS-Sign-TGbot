# NS-Sign-TGbot 部署到 Debian 12（docker部署流程）

> 项目目录：/opt/NodeSeek  
> 需要准备：TG_BOT_TOKEN、ADMIN_IDS（你的TG用户ID）、CLIENT_KEY（cloudflyer 的 -K）

---

## 1) 安装依赖
~~~bash
sudo apt update
sudo apt -y install git curl vim ca-certificates \
  python3 python3-venv python3-pip \
  nodejs npm \
  docker.io
sudo systemctl enable --now docker
~~~

---

## 2) 拉取项目
~~~bash
sudo mkdir -p /opt/NodeSeek
cd /opt/NodeSeek
sudo git clone https://github.com/yx617012389/NS-Sign-TGbot .
~~~

---

## 3) 启动 cloudflyer（127.0.0.1:3000）
~~~bash
sudo docker rm -f cloudflyer 2>/dev/null || true
sudo docker run -d \
  --name cloudflyer \
  -p 127.0.0.1:3000:3000 \
  -v /opt/NodeSeek:/opt/NodeSeek \
  --restart unless-stopped \
  jackzzs/cloudflyer \
  -K 你的CLIENT_KEY \
  -H 0.0.0.0 \
  -P 3000
~~~

---

## 4) 启动 FlareSolverr（127.0.0.1:8191）
~~~bash
sudo docker rm -f flaresolverr 2>/dev/null || true
sudo docker run -d \
  --name flaresolverr \
  --network host \
  --restart unless-stopped \
  -e LOG_LEVEL=info \
  ghcr.io/flaresolverr/flaresolverr:latest
~~~

---

## 5) 安装 Node 依赖
~~~bash
cd /opt/NodeSeek
npm install
# 如果 npm install 失败且报缺 cloudscraper：
# npm i cloudscraper
~~~

---

## 6) 安装 Python 依赖（venv）
~~~bash
cd /opt/NodeSeek
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
~~~

---

## 7) 配置 .env（用 vim）
~~~bash
cd /opt/NodeSeek
vim .env
~~~

把下面内容粘进去并按实际替换：
~~~conf
API_BASE_URL=http://127.0.0.1:3000
FLARESOLVERR_URL=http://127.0.0.1:8191/v1
CLIENT_KEY=你的CLIENT_KEY
TG_BOT_TOKEN=你的TG_BOT_TOKEN
ADMIN_IDS=你的TG用户ID(多个用逗号分隔)
GROUPS=群ID(可选,多个用逗号分隔)
~~~

保存权限：
~~~bash
chmod 600 /opt/NodeSeek/.env
~~~

---

## 8) 手动启动测试
~~~bash
cd /opt/NodeSeek
source .venv/bin/activate
python3 bot.py
~~~

---

## 9) systemd 自启
编辑服务文件：
~~~bash
sudo vim /etc/systemd/system/nodeseek.service
~~~

写入：
~~~ini
[Unit]
Description=NodeSeek Bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/NodeSeek
EnvironmentFile=/opt/NodeSeek/.env
ExecStart=/opt/NodeSeek/.venv/bin/python3 /opt/NodeSeek/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
~~~

启用并启动：
~~~bash
sudo systemctl daemon-reload
sudo systemctl enable --now nodeseek
sudo systemctl status nodeseek --no-pager
sudo journalctl -u nodeseek -f
~~~
