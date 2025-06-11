FROM python:3.12.10-slim

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . .

RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /code

USER appuser

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 CMD curl -f http://0.0.0.0:7860/ || exit 1

CMD ["python", "app.py"]

