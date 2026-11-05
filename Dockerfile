FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY switchyard ./switchyard

RUN useradd --create-home switchyard && chown -R switchyard /app
USER switchyard

EXPOSE 8000
CMD ["uvicorn", "switchyard.main:app", "--host", "0.0.0.0", "--port", "8000"]
