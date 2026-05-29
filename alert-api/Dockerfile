FROM python:3.12-slim
WORKDIR /app
# Install from the repo-level runtime dependency set shared with local dev tooling.
COPY requirements/normal.txt /tmp/requirements-normal.txt
RUN pip install --no-cache-dir -r /tmp/requirements-normal.txt
COPY alert-api /app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "debug"]
