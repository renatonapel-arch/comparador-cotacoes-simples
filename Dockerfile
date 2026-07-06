FROM python:3.12-slim

# Coolify checa a saúde do container rodando curl/wget DE DENTRO dele —
# sem isso o container novo sempre fica "unhealthy" e o deploy volta pro antigo.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY app.py postgres_backend.py extracao.py ./
COPY static ./static

ENV COMPARADOR_DB=postgres
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
