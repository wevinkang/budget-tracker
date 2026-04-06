FROM python:3.11-slim

# sqlcipher3 requires the native SQLCipher library
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlcipher-dev \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directories — mount volumes here to persist across restarts
RUN mkdir -p /data /imports/done
ENV BUDGET_DB=/data/budget.db
ENV BUDGET_IMPORTS=/imports

EXPOSE 5000

CMD ["python", "app.py"]
