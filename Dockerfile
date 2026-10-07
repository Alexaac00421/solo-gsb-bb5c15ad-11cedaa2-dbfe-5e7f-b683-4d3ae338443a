FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    APP_DB_PATH=/data/routing.db

WORKDIR /srv

# 先装依赖，利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh && mkdir -p /data

# 数据卷：SQLite 数据库与 WAL 文件
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import json,urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('APP_PORT','8080')+'/health',timeout=3); \
assert json.load(r)['status']=='ok'" || exit 1

# 启动即建库 + 导入示例数据（可用环境变量覆盖为自定义文件），然后起服务
ENTRYPOINT ["./docker-entrypoint.sh"]
