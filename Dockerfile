FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adapter.py .

ENV HOST=0.0.0.0
ENV PORT=8045

EXPOSE 8045

CMD ["python", "adapter.py"]
