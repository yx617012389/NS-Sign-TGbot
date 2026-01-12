1、安装Docker


2、Docker 安装 cloudflyer

docker run -itd \
  --name cloudflyer \
  -p 3000:3000 \
  --restart unless-stopped \
  jackzzs/cloudflyer \
  -K 你的秘钥 \
  -H 0.0.0.0

秘钥随机生成，完成后输出到控制台，并且保存到/opt/NodeSeek目录下的cloudflyer_Key.txt，如果路径不存在就创建/opt/NodeSeek目录

3、Docker 安装 flaresolverr，API 调用地址 http://127.0.0.1:8191/v1

docker run -d \
  --name flaresolverr \
  --network host \
  -e LOG_LEVEL=info \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest

4、验证：
curl http://localhost:3000   # CloudFreed
curl http://localhost:8191/health  # FlareSolverr

5、创建.env文件
...
API_BASE_URL=http://127.0.0.1:3000
FLARESOLVERR_URL=http://127.0.0.1:8191/v1
CLIENT_KEY=你的密钥
TG_BOT_TOKEN=
ADMIN_IDS=
GROUPS=
...
