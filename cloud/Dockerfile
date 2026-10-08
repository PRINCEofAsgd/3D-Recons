# 演示镜像版本由 Workflow 参数固定；容器只安装云端算法依赖。
FROM python:3.11.13-slim-bookworm
WORKDIR /app
ENV QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTHONUNBUFFERED=1
# Linux ARM64 无需桌面 Qt；固定已验收依赖，并把依赖层置于源码层之前。
COPY cloud/requirements-runtime.txt /app/requirements-runtime.txt
RUN pip install --no-cache-dir -r /app/requirements-runtime.txt
COPY code/ /app/code/
RUN pip install --no-cache-dir --no-deps /app/code
CMD ["python", "-m", "store_vision.cloud_pipeline", "--help"]
