FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && python -m pip install --no-cache-dir pytest==8.4.2 freezegun==1.5.5 click==8.1.8 pretend tomli-w mdurl "markdown-it-py<4" pytest-regressions pytest-timeout && python -m pip freeze > /opt/issuepilot-requirements.lock
RUN mkdir -p /opt/issuepilot /patches /work && touch /patches/change.patch && chmod 644 /patches/change.patch && chmod 755 /opt/issuepilot /patches && chmod 1777 /work
COPY sandbox/runner.py /opt/issuepilot/runner.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" PYTHONUNBUFFERED=1
WORKDIR /work
USER 65534:65534
