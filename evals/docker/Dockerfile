FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONNOUSERSITE=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY evals/docker/requirements.lock /tmp/requirements.lock
COPY pyproject.toml README.md /opt/codepilot-src/
COPY codepilot_s20 /opt/codepilot-src/codepilot_s20
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && python -m pip install --no-cache-dir /opt/codepilot-src \
    && groupadd --gid 10001 eval \
    && useradd --uid 10001 --gid 10001 --create-home eval \
    && mkdir -p /workspace /state /runtime /broker/requests /broker/responses /trusted_eval /grading_workspace /inputs \
    && touch /inputs/trace.jsonl /inputs/final.md /inputs/stdout.txt /inputs/stderr.txt \
    && chown -R 10001:10001 /workspace /state /runtime /broker /trusted_eval /grading_workspace /inputs \
    && rm -rf /tmp/requirements.lock /opt/codepilot-src

WORKDIR /workspace
USER 10001:10001
CMD ["python", "-m", "codepilot_s20.eval_container_entry"]
