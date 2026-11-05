# Khoj A2A agent image. Build context is the REPO ROOT: the agent reuses
# app/auth.py (token verification) from the web service.
FROM python:3.12-slim
WORKDIR /srv
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/__init__.py app/orgs.py app/auth.py ./app/
COPY agent/main.py ./agent_main.py
ENV PORT=8080
CMD exec uvicorn agent_main:app --host 0.0.0.0 --port ${PORT}
