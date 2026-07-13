FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONNOUSERSITE=1

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && groupadd --gid 10001 eval \
    && useradd --uid 10001 --gid 10001 --create-home eval \
    && mkdir -p /trusted_eval /grading_workspace /inputs \
    && touch /inputs/trace.jsonl /inputs/final.md /inputs/stdout.txt /inputs/stderr.txt \
    && chown -R 10001:10001 /trusted_eval /grading_workspace /inputs \
    && rm /tmp/requirements.lock

WORKDIR /workspace
USER 10001:10001
CMD ["sleep", "infinity"]
