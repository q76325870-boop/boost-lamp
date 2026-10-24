FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home boosty && mkdir /state && chown boosty:boosty /state
COPY server ./server
COPY plugin ./plugin
USER 10001:10001
EXPOSE 8080 8081
CMD ["python", "-m", "server"]
